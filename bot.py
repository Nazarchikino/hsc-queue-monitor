import asyncio
import logging
import os
import time
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import aiohttp
from playwright.async_api import async_playwright
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# 1. КОНФІГУРАЦІЯ ТА ЗМІННІ СЕРЕДОВИЩА
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHAT_ID = int(os.getenv("CHAT_ID", "0"))

KEY_FILE_PATH = os.getenv("KEY_FILE_PATH", "key.dat")
KEY_PASSWORD = os.getenv("KEY_PASSWORD", "")
CA_NAME_EXACT = os.getenv("CA_NAME_EXACT", 'КНЕДП АЦСК АТ КБ "ПриватБанк"')

SHOW_BROWSER = False  # True для наочного дебагу вікна

# Динамічні параметри моніторингу
TARGET_DEPT_ID = int(os.getenv("DEFAULT_DEPT_ID", "18"))
SERVICE_ID = int(os.getenv("DEFAULT_SERVICE_ID", "52"))

# Довідник типових послуг практичних іспитів
SERVICES_DICT = {
    49: "🚗 Кат. B (МЕХАНІКА)",
    50: "🚗 Кат. B (АВТОМАТ)",
    51: "🚚 Кат. C1",
    52: "🚚 Кат. C",
    57: "🚛 Кат. CE",
    47: "🏍 Кат. A (МЕХАНІКА)",
    65: "🏍 Кат. A (АВТОМАТ)",
    46: "🛵 Кат. A1",
    53: "🚌 Кат. D1",
    8: "📖 Теоретичний іспит"
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:154.0) Gecko/20100101 Firefox/154.0",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://eqn.hsc.gov.ua/",
    "Connection": "keep-alive"
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
current_cookies = {}
auth_lock = asyncio.Lock()
last_slot_state = False
last_auth_time = 0


# ==========================================
# 2. АВТОРИЗАЦІЯ (PLAYWRIGHT FIREFOX)
# ==========================================
async def perform_auto_auth() -> dict:
    """Виконує автоматичний вхід через КЕП у headless-режимі."""
    async with auth_lock:
        if not os.path.exists(KEY_FILE_PATH):
            logging.error(f"[Auth] Файл ключа '{KEY_FILE_PATH}' не знайдено!")
            return {}

        logging.info(f"[Auth] Запуск Firefox (відображення: {SHOW_BROWSER})...")
        async with async_playwright() as p:
            browser = await p.firefox.launch(
                headless=not SHOW_BROWSER,
                slow_mo=200 if SHOW_BROWSER else 0
            )
            
            context = await browser.new_context(
                user_agent=HEADERS["User-Agent"],
                locale="uk-UA",
                timezone_id="Europe/Kyiv"
            )
            page = await context.new_page()

            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
            """)

            try:
                logging.info("[Auth] Відкриваємо стартову сторінку...")
                await page.goto("https://eqn.hsc.gov.ua/", wait_until="domcontentloaded", timeout=60000)

                # Чекбокс згоди
                checkbox = page.locator("input[type='checkbox']").first
                if await checkbox.count() > 0:
                    await checkbox.check(force=True)
                else:
                    await page.get_by_text("Я ознайомлений").first.click(force=True)
                await page.wait_for_timeout(800)

                # Кнопка ID.GOV.UA
                id_btn = page.locator("button:has(svg#svg900), button:has(title:has-text('id.gov.ua'))").first
                if await id_btn.count() > 0:
                    await id_btn.click(force=True)
                else:
                    await page.locator("button.MuiButton-containedPrimary").first.click(force=True)

                # ID.GOV.UA
                await page.wait_for_url("**/id.gov.ua/**", timeout=35000)
                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_timeout(1000)

                # Вкладка Файловий
                await page.get_by_text("Файловий").first.click()
                await page.wait_for_timeout(800)

                # Вибір АЦСК
                select_elem = page.locator("div, select, button").filter(has_text="Вчасно").first
                if await select_elem.count() > 0:
                    await select_elem.click()
                else:
                    try:
                        await page.select_option("select", label=CA_NAME_EXACT)
                    except Exception:
                        pass

                # Ключ і пароль
                await page.set_input_files('input[type="file"]', KEY_FILE_PATH)
                await page.wait_for_timeout(800)
                await page.fill('input[type="password"]', KEY_PASSWORD)
                await page.wait_for_timeout(500)

                # Зчитування
                await page.locator("button:has-text('Продовжити')").first.click()
                await page.wait_for_timeout(2500)

                # Згода з персональними даними
                second_btn = page.locator("button:has-text('Продовжити'), button:has-text('Погодитись'), button:has-text('Увійти')").first
                if await second_btn.count() > 0 and await second_btn.is_visible():
                    await second_btn.click()

                # Перехід у кабінет
                await page.wait_for_url("**/cabinet**", timeout=45000)
                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_timeout(2000)

                queue_link = page.locator('a[href*="/cabinet/queue"]').first
                if await queue_link.count() > 0:
                    await queue_link.click(force=True)
                else:
                    card = page.locator("div, a, button").filter(has_text="Записатись у чергу").last
                    if await card.count() > 0:
                        await card.evaluate("node => node.click()")
                    else:
                        await page.get_by_text("Записатись у чергу").first.click(force=True)

                try:
                    await page.wait_for_url("**/cabinet/queue**", timeout=25000)
                except Exception:
                    await page.wait_for_timeout(3000)

                await page.wait_for_load_state("networkidle")
                await page.wait_for_timeout(1500)

                cookies_list = await context.cookies()
                new_cookies = {c['name']: c['value'] for c in cookies_list}

                logging.info("[Auth] Сесію успішно оновлено!")
                await browser.close()
                return new_cookies

            except Exception as e:
                logging.error(f"[Auth] Помилка авторизації: {e}")
                await page.screenshot(path="auth_error.png")
                await browser.close()
                return {}


# ==========================================
# 3. ДОПОМІЖНІ ФУНКЦІЇ ДАНИХ
# ==========================================
def extract_departments(data) -> list:
    """Універсальне розгортання структур відповідей API."""
    result = []
    if isinstance(data, dict):
        data = list(data.values())
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                result.append(item)
            elif isinstance(item, list):
                result.extend(extract_departments(item))
    return result

async def fetch_departments() -> list:
    """Завантажує актуальний перелік ТСЦ для обраної послуги."""
    departments_url = f"https://eqn.hsc.gov.ua/api/v2/equeue/departments?serviceId={SERVICE_ID}"
    if not current_cookies:
        return []
    try:
        async with aiohttp.ClientSession(headers=HEADERS, cookies=current_cookies, timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.get(departments_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return extract_departments(data)
    except Exception as e:
        logging.error(f"[API] Помилка отримання ТСЦ: {e}")
    return []


# ==========================================
# 4. ФОНОВИЙ МОНІТОРИНГ
# ==========================================
async def queue_monitor():
    global current_cookies, last_slot_state, last_auth_time
    SESSION_LIFETIME = 13 * 60
    last_auth_time = 0

    current_cookies = await perform_auto_auth()
    if current_cookies:
        last_auth_time = time.time()

    logging.info("[Monitor] Фоновий моніторинг запущено.")
    client_timeout = aiohttp.ClientTimeout(total=10)

    while True:
        try:
            now = time.time()
            if (now - last_auth_time) >= SESSION_LIFETIME:
                logging.info("[Monitor] Планове оновлення сесії...")
                new_c = await perform_auto_auth()
                if new_c:
                    current_cookies = new_c
                    last_auth_time = time.time()

            if current_cookies:
                departments_url = f"https://eqn.hsc.gov.ua/api/v2/equeue/departments?serviceId={SERVICE_ID}"
                async with aiohttp.ClientSession(headers=HEADERS, cookies=current_cookies, timeout=client_timeout) as session:
                    async with session.get(departments_url, allow_redirects=False) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            dept_list = extract_departments(data)

                            for dept in dept_list:
                                try:
                                    d_id = int(dept.get("id"))
                                except (ValueError, TypeError):
                                    d_id = dept.get("id")

                                if d_id == int(TARGET_DEPT_ID):
                                    count = dept.get("allowOnlineCount", 0)
                                    name = dept.get("name", "ТСЦ")
                                    city = dept.get("city", "")
                                    srv_name = SERVICES_DICT.get(SERVICE_ID, f"ID {SERVICE_ID}")

                                    if count > 0:
                                        if not last_slot_state:
                                            msg = (
                                                f"🚨 <b>З'ЯВИВСЯ ТАЛОН!</b>\n\n"
                                                f"📌 <b>Послуга:</b> {srv_name}\n"
                                                f"🏢 <b>Центр:</b> {name} ({city})\n"
                                                f"📊 <b>Кількість місць:</b> {count}\n"
                                                f"📍 <b>Адреса:</b> {dept.get('street')} {dept.get('building')}\n\n"
                                                f"👉 <a href='https://eqn.hsc.gov.ua/cabinet/queue'>ТЕРМІНОВО ЗАПИСАТИСЯ</a>"
                                            )
                                            await bot.send_message(CHAT_ID, msg, parse_mode=ParseMode.HTML)
                                            last_slot_state = True
                                        logging.info(f"🔥 [{name} | ID:{d_id}] Знайдено слотів: {count}")
                                    else:
                                        last_slot_state = False
                                        logging.info(f"✅ [{name} | ID:{d_id}] 200 OK. Слоти: 0. Моніторинг активний...")
                                    break

                        elif resp.status in (401, 403, 301, 302, 307):
                            logging.warning(f"[Monitor] Сесія застаріла ({resp.status}). Переавторизація...")
                            new_c = await perform_auto_auth()
                            if new_c:
                                current_cookies = new_c
                                last_auth_time = time.time()
                            else:
                                await asyncio.sleep(20)

            else:
                current_cookies = await perform_auto_auth()
                if current_cookies:
                    last_auth_time = time.time()
                await asyncio.sleep(10)

        except asyncio.TimeoutError:
            logging.warning("[Monitor] Таймаут відповіді API.")
        except Exception as e:
            logging.error(f"[Monitor] Помилка циклу: {e}")

        await asyncio.sleep(15)


# ==========================================
# 5. КЛАВІАТУРИ ТА ОБРОБНИКИ ТЕЛЕГРАМ
# ==========================================
def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Головне меню керування."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 Обрати ТСЦ (за областями)", callback_data="menu_regions")],
        [InlineKeyboardButton(text="🚗 Змінити вид послуги", callback_data="menu_services")],
        [InlineKeyboardButton(text="📊 Поточний статус", callback_data="menu_status")],
        [InlineKeyboardButton(text="🔄 Оновити сесію КЕП", callback_data="menu_reauth")]
    ])

@dp.message(Command("start", "menu"))
async def cmd_start(message: types.Message):
    srv_title = SERVICES_DICT.get(SERVICE_ID, f"ID {SERVICE_ID}")
    await message.answer(
        "👋 <b>Панель керування моніторингом ГСЦ МВС</b>\n\n"
        f"🎯 <b>Поточний ТСЦ ID:</b> <code>{TARGET_DEPT_ID}</code>\n"
        f"📌 <b>Поточна послуга:</b> <code>{srv_title}</code>\n\n"
        "Скористайся кнопками нижче для налаштування:",
        reply_markup=get_main_menu_keyboard(),
        parse_mode=ParseMode.HTML
    )

# --- МЕНЮ ВИБОРУ ПОСЛУГИ ---
@dp.callback_query(F.data == "menu_services")
async def cb_services(callback: types.CallbackQuery):
    buttons = []
    for s_id, s_name in SERVICES_DICT.items():
        prefix = "✅ " if s_id == SERVICE_ID else ""
        buttons.append([InlineKeyboardButton(text=f"{prefix}{s_name}", callback_data=f"set_service_{s_id}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_back")])

    await callback.message.edit_text(
        "⚙️ <b>Оберіть категорію або тип послуги:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("set_service_"))
async def cb_set_service(callback: types.CallbackQuery):
    global SERVICE_ID, last_slot_state
    SERVICE_ID = int(callback.data.replace("set_service_", ""))
    last_slot_state = False
    srv_title = SERVICES_DICT.get(SERVICE_ID, f"ID {SERVICE_ID}")

    await callback.message.edit_text(
        f"✅ <b>Послугу успішно змінено!</b>\n\n"
        f"📌 Активна послуга: <b>{srv_title}</b> (ID: <code>{SERVICE_ID}</code>)\n"
        f"🎯 Цільовий ТСЦ: <code>{TARGET_DEPT_ID}</code>",
        reply_markup=get_main_menu_keyboard(),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()

# --- МЕНЮ ВИБОРУ ОБЛАСТІ ТА ТСЦ ---
@dp.callback_query(F.data == "menu_regions")
async def cb_regions(callback: types.CallbackQuery):
    await callback.message.edit_text("⏳ Завантаження списку центрів...")
    departments = await fetch_departments()
    if not departments:
        await callback.message.edit_text(
            "⚠️ Не вдалося отримати дані ТСЦ. Зачекайте ініціалізації сесії.",
            reply_markup=get_main_menu_keyboard()
        )
        await callback.answer()
        return

    # Збираємо унікальні області
    regions = sorted(list({d.get("region", "Інше") for d in departments if d.get("region")}))

    buttons = []
    row = []
    for reg in regions:
        # Скорочуємо назву для гарного вигляду кнопок
        short_name = reg.replace(" область", "").replace("ська", "ськ.").replace("цька", "цьк.")
        row.append(InlineKeyboardButton(text=short_name, callback_data=f"reg_{reg[:20]}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_back")])

    await callback.message.edit_text(
        "📍 <b>Оберіть область для пошуку ТСЦ:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("reg_"))
async def cb_departments_by_region(callback: types.CallbackQuery):
    reg_prefix = callback.data.replace("reg_", "")
    departments = await fetch_departments()

    matching_depts = [d for d in departments if d.get("region", "").startswith(reg_prefix)]

    if not matching_depts:
        await callback.message.edit_text(
            "⚠️ Центрів у цій області не знайдено.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ До областей", callback_data="menu_regions")]
            ])
        )
        await callback.answer()
        return

    buttons = []
    for d in matching_depts:
        d_id = d.get("id")
        name = d.get("name", f"ТСЦ {d_id}")
        city = d.get("city", "")
        prefix = "✅ " if int(d_id) == TARGET_DEPT_ID else ""
        btn_text = f"{prefix}{name} ({city})"
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"set_dept_{d_id}")])

    buttons.append([InlineKeyboardButton(text="⬅️ До областей", callback_data="menu_regions")])

    await callback.message.edit_text(
        f"🏢 <b>Оберіть сервісний центр:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("set_dept_"))
async def cb_set_department(callback: types.CallbackQuery):
    global TARGET_DEPT_ID, last_slot_state
    TARGET_DEPT_ID = int(callback.data.replace("set_dept_", ""))
    last_slot_state = False

    departments = await fetch_departments()
    target_info = next((d for d in departments if int(d.get("id", 0)) == TARGET_DEPT_ID), None)
    name = target_info.get("name", "ТСЦ") if target_info else f"ID {TARGET_DEPT_ID}"
    city = target_info.get("city", "") if target_info else ""

    await callback.message.edit_text(
        f"🎯 <b>Цільовий центр успішно змінено!</b>\n\n"
        f"🏢 <b>Центр:</b> {name} ({city})\n"
        f"🔢 <b>ID:</b> <code>{TARGET_DEPT_ID}</code>\n"
        f"📌 <b>Послуга:</b> {SERVICES_DICT.get(SERVICE_ID, f'ID {SERVICE_ID}')}",
        reply_markup=get_main_menu_keyboard(),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()

@dp.callback_query(F.data == "menu_status")
async def cb_status(callback: types.CallbackQuery):
    departments = await fetch_departments()
    target = next((d for d in departments if int(d.get("id", 0)) == TARGET_DEPT_ID), None)

    if target:
        count = target.get("allowOnlineCount", 0)
        await callback.message.edit_text(
            f"📊 <b>Поточний стан моніторингу:</b>\n\n"
            f"🏢 <b>Центр:</b> {target.get('name')} ({target.get('city')})\n"
            f"📍 <b>Адреса:</b> {target.get('street')} {target.get('building')}\n"
            f"📌 <b>Послуга:</b> {SERVICES_DICT.get(SERVICE_ID, f'ID {SERVICE_ID}')}\n"
            f"🎟 <b>Вільних талонів:</b> <b>{count} шт.</b>",
            reply_markup=get_main_menu_keyboard(),
            parse_mode=ParseMode.HTML
        )
    else:
        await callback.message.edit_text(
            f"⚠️ ТСЦ <code>{TARGET_DEPT_ID}</code> наразі не відповідає або сесія оновлюється.",
            reply_markup=get_main_menu_keyboard(),
            parse_mode=ParseMode.HTML
        )
    await callback.answer()

@dp.callback_query(F.data == "menu_reauth")
async def cb_reauth(callback: types.CallbackQuery):
    global current_cookies, last_auth_time
    await callback.message.edit_text("🔄 Запуск фонового оновлення сесії через КЕП...")
    new_c = await perform_auto_auth()
    if new_c:
        current_cookies = new_c
        last_auth_time = time.time()  # Скидаємо таймер 13 хвилин
        await callback.message.edit_text("✅ Сесію успішно оновлено!", reply_markup=get_main_menu_keyboard())
    else:
        await callback.message.edit_text("❌ Помилка авторизації. Перевірте логи.", reply_markup=get_main_menu_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "menu_back")
async def cb_back(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "👋 <b>Головне меню моніторингу ГСЦ МВС:</b>",
        reply_markup=get_main_menu_keyboard(),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()


# ==========================================
# 6. ТОЧКА ВХОДУ
# ==========================================
async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    asyncio.create_task(queue_monitor())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
