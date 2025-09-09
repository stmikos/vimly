# -*- coding: utf-8 -*-
"""
Vimly — Client Demo Bot (FastAPI + aiogram 3.7+)
Правки:
- Перешёл на HTML parse mode (устраняет ошибки Markdown с подчёркиваниями и т.п.)
- Безопасное редактирование сообщений (edit_text vs edit_caption)
- /start: картинка отдельно, меню отдельным текстом (как ты просил)
- WebApp-квиз остаётся
"""

# --- imports ---
import os, logging, re, asyncio, json, html
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, Update,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    FSInputFile,
)
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

# --- env ---
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

def _norm_base_url(s: str) -> str:
    s = (s or "").strip()
    return s[:-1] if s.endswith("/") else s

def _norm_path(p: str) -> str:
    p = (p or "").strip()
    return p if p.startswith("/") else f"/{p}"

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN env var")

ADMIN_CHAT_ID = int((os.getenv("ADMIN_CHAT_ID") or "0").strip() or "0")
LEADS_CHAT_ID = int((os.getenv("LEADS_CHAT_ID") or "0").strip() or "0")

BASE_URL = _norm_base_url(os.getenv("BASE_URL"))
WEBHOOK_PATH = _norm_path(os.getenv("WEBHOOK_PATH") or "/telegram/webhook/vimly")
WEBHOOK_SECRET = (os.getenv("WEBHOOK_SECRET") or "").strip()
MODE = (os.getenv("MODE") or "webhook").strip().lower()  # webhook | polling

# --- branding ---
BRAND_NAME = (os.getenv("BRAND_NAME") or "Vimly").strip()
BRAND_TAGLINE = (os.getenv("BRAND_TAGLINE") or "Боты, которые продают").strip()
BRAND_TG = (os.getenv("BRAND_TG") or "@Vimly_bot").strip()
BRAND_SITE = (os.getenv("BRAND_SITE") or "").strip()

# --- logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("vimly-webapp-demo")

# --- aiogram 3.7+ init: HTML parse mode ---
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()  # объявлен до хендлеров

# ---- STORE ----
class Store:
    accepting = True
    stats = {"starts": 0, "quiz": 0, "orders": 0, "webquiz": 0}

# ---- FSM ----
class Quiz(StatesGroup):
    niche = State()
    goal = State()
    deadline = State()

class Order(StatesGroup):
    contact = State()

# ---- helpers ----
def esc(s: Optional[str]) -> str:
    return html.escape(s or "", quote=False)

def header() -> str:
    parts = [f"<b>{esc(BRAND_NAME)}</b>", esc(BRAND_TAGLINE)]
    if BRAND_SITE:
        parts.append(esc(BRAND_SITE))
    return "\n".join(parts)

def ufmt(m: Message) -> str:
    user = m.from_user
    tag = f"@{user.username}" if user.username else f"id={user.id}"
    return esc(f"{user.full_name} ({tag})")

async def notify_admin(text: str):
    if ADMIN_CHAT_ID:
        try:
            await bot.send_message(ADMIN_CHAT_ID, text, disable_notification=True)
        except Exception as e:
            log.warning("notify_admin failed: %s", e)
    if LEADS_CHAT_ID:
        try:
            await bot.send_message(LEADS_CHAT_ID, text)
        except Exception as e:
            log.warning("notify_leads failed: %s", e)

async def safe_edit(c: CallbackQuery, html_text: str, kb: InlineKeyboardMarkup | None = None):
    """Акуратно редактируем: если сообщение было медиа — меняем подпись; если нельзя — шлём новое."""
    kb = kb or main_kb()
    m = c.message
    try:
        if getattr(m, "content_type", None) in {"photo","video","animation","document","audio","voice","video_note"}:
            await m.edit_caption(caption=html_text, reply_markup=kb)
        else:
            await m.edit_text(html_text, reply_markup=kb)
    except TelegramBadRequest:
        await m.answer(html_text, reply_markup=kb)

# ---- UI ----
def main_kb() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="🧭 Процесс", callback_data="go_process"),
            InlineKeyboardButton(text="💼 Кейсы (демо)", callback_data="go_cases"),
        ],
        [
            InlineKeyboardButton(text="🧪 Квиз-заявка", callback_data="go_quiz"),
            InlineKeyboardButton(text="💸 Пакеты и цены", callback_data="go_prices"),
        ],
        [
            InlineKeyboardButton(text="🛒 Заказать", callback_data="go_order"),
            InlineKeyboardButton(text="📬 Контакты", callback_data="go_contacts"),
        ],
        [
            InlineKeyboardButton(text="📝 Бриф (7 вопросов)", callback_data="go_brief"),
            InlineKeyboardButton(text="🎁 Подарок", callback_data="go_gift"),
        ],
    ]
   # где формируется меню
if BASE_URL:
    rows.append([InlineKeyboardButton(
        text="🧪 WebApp-квиз",
        web_app=WebAppInfo(url=f"{BASE_URL}/webapp/quiz/")  # <-- слэш!
    )])

    else:
        rows.append([InlineKeyboardButton(text="🧪 WebApp-квиз (скоро)", callback_data="go_webapp_na")])
    rows.append([InlineKeyboardButton(text="🛠 Админ", callback_data="admin_open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_kb() -> InlineKeyboardMarkup:
    on = "🟢" if Store.accepting else "🔴"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"{on} Приём заявок", callback_data="admin_toggle"),
            InlineKeyboardButton(text="📈 Статистика", callback_data="admin_stats"),
        ],
        [
            InlineKeyboardButton(text="📣 Тест-рассылка", callback_data="admin_broadcast"),
            InlineKeyboardButton(text="⬅️ Меню", callback_data="go_menu"),
        ]
    ])

# ---- HANDLERS ----
@dp.message(CommandStart())
async def on_start(m: Message):
    Store.stats["starts"] += 1
    # 1) hero (без кнопок)
    hero = os.path.join(os.path.dirname(__file__), "assets", "hero.png")
    try:
        await m.answer_photo(FSInputFile(hero), caption=header())
    except Exception:
        pass
    # 2) текст + клавиатура (редактируем дальше)
    welcome = (
        "Этот бот — <b>демо для клиентов</b>: меню, кейсы, квиз и запись в 2 клика.\n"
        "Нажмите кнопку ниже 👇"
    )
    await m.answer(welcome, reply_markup=main_kb())

@dp.message(Command("menu"))
async def on_menu(m: Message):
    await m.answer("Главное меню:", reply_markup=main_kb())

@dp.message(Command("admin"))
async def on_admin(m: Message):
    if m.from_user.id != ADMIN_CHAT_ID:
        return await m.answer("Админ-панель доступна владельцу бота.")
    await m.answer("Админ-панель:", reply_markup=admin_kb())

@dp.callback_query(F.data == "go_webapp_na")
async def cb_webapp_na(c: CallbackQuery):
    await c.answer("Веб-форма включится после настройки BASE_URL.", show_alert=True)

@dp.callback_query(F.data == "go_menu")
async def cb_menu(c: CallbackQuery):
    await safe_edit(c, "Главное меню:", kb=main_kb()); await c.answer()

@dp.callback_query(F.data == "go_process")
async def cb_process(c: CallbackQuery):
    txt = (
        "Как запускаем за 1–3 дня:\n"
        "1) <b>Созвон 15 минут</b> — фиксируем цели\n"
        "2) <b>MVP</b> — меню + квиз + админ-чат\n"
        "3) <b>Запуск</b> — подключаем Sheets/оплату/канал\n"
        "4) <b>Поддержка</b> — рассылки, правки, отчёты\n\n"
        "Сроки и бюджет фиксируем письменно."
    )
    await safe_edit(c, txt); await c.answer()

@dp.callback_query(F.data == "go_cases")
async def cb_cases(c: CallbackQuery):
    txt = (
        "Кейсы (демо):\n"
        "• Барбершоп — запись и отзывы, 2 экрана, +26 заявок/мес\n"
        "• Пекарня — квиз + купоны, ~18% конверсия в визит\n"
        "• Автор-канал — оплата → доступ в закрытый чат\n"
        "• Коворкинг — афиша/RSVP, считает гостей и выгружает список\n\n"
        "Покажу живые прототипы на созвоне."
    )
    await safe_edit(c, txt); await c.answer()

@dp.callback_query(F.data == "go_prices")
async def cb_prices(c: CallbackQuery):
    txt = (
        "<b>Пакеты и цены:</b>\n\n"
        "• <b>Lite</b> — 15–20k ₽: меню/квиз/заявки, без БД и оплаты\n"
        "• <b>Standard</b> — 25–45k ₽: + Google Sheets, админ-панель, напоминания\n"
        "• <b>Pro</b> — 50–90k ₽: + оплата, доступ в канал, логи, бэкапы\n\n"
        "<i>Поддержка 3–10k ₽/мес</i>: правки, рассылки, мониторинг"
    )
    await safe_edit(c, txt); await c.answer()

@dp.callback_query(F.data == "go_contacts")
async def cb_contacts(c: CallbackQuery):
    txt = (
        "<b>Контакты:</b>\n"
        f"Telegram: {esc(BRAND_TG)}\n"
        f"Сайт/портфолио: {esc(BRAND_SITE) or '—'}\n\n"
        "Оставьте телефон — свяжемся в удобное время."
    )
    await safe_edit(c, txt); await c.answer()

@dp.callback_query(F.data == "go_brief")
async def cb_brief(c: CallbackQuery):
    brief = (
        "<b>Мини-бриф (7 вопросов):</b>\n"
        "1) Ниша и город\n"
        "2) Цель бота (заявки/запись/оплата/отзывы)\n"
        "3) Кнопки меню (4–6)\n"
        "4) Что слать в админ-чат (лиды/фото/файлы)\n"
        "5) Нужны ли Google Sheets и рассылки\n"
        "6) Нужна ли оплата и доступ в канал\n"
        "7) Срок запуска и бюджет"
    )
    await safe_edit(c, brief); await c.answer()

# --- Классический квиз (в чате) ---
@dp.callback_query(F.data == "go_quiz")
async def quiz_start(c: CallbackQuery, state: FSMContext):
    if not Store.accepting:
        return await c.answer("Приём заявок временно закрыт", show_alert=True)
    await state.set_state(Quiz.niche)
    await safe_edit(c, "🧪 Квиз: ваша ниша и город? (1/3)", kb=None)
    await c.answer()

@dp.message(Quiz.niche)
async def quiz_niche(m: Message, state: FSMContext):
    await state.update_data(niche=(m.text or "").strip()[:200])
    await state.set_state(Quiz.goal)
    await m.answer("Цель бота? (2/3) — заявки, запись, оплата, отзывы…")

@dp.message(Quiz.goal)
async def quiz_goal(m: Message, state: FSMContext):
    await state.update_data(goal=(m.text or "").strip()[:300])
    await state.set_state(Quiz.deadline)
    await m.answer("Срок запуска? (3/3) — например: 2–3 дня / дата")

@dp.message(Quiz.deadline)
async def quiz_done(m: Message, state: FSMContext):
    data = await state.update_data(deadline=(m.text or "").strip()[:100])
    await state.clear()
    Store.stats["quiz"] += 1
    await m.answer((
        "Спасибо! Заявка получена 🎉\n\n"
        f"Ниша: {esc(data.get('niche'))}\n"
        f"Цель: {esc(data.get('goal'))}\n"
        f"Срок: {esc(data.get('deadline'))}\n\n"
        "Свяжемся в ближайшее время."
    ), reply_markup=main_kb())
    await notify_admin((
        "🆕 Заявка (квиз-чат)\n"
        f"От: {ufmt(m)}\n"
        f"Ниша: {esc(data.get('niche'))}\n"
        f"Цель: {esc(data.get('goal'))}\n"
        f"Срок: {esc(data.get('deadline'))}\n"
        f"UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
    ))

# --- Приём данных из WebApp ---
@dp.message(F.web_app_data)
async def on_webapp_data(m: Message):
    Store.stats["webquiz"] += 1
    raw = m.web_app_data.data
    try:
        data = json.loads(raw)
    except Exception:
        data = {"raw": raw}
    comp = (data.get("company") or "").strip()[:2000]
    task = (data.get("task") or "").strip()[:2000]
    contact = (data.get("contact") or "").strip()[:200]

    txt = (
        "🧪 Заявка (WebApp)\n"
        f"От: {ufmt(m)}\n"
        f"Компания: {esc(comp) or '—'}\n"
        f"Задача: {esc(task) or '—'}\n"
        f"Контакт: {esc(contact) or '—'}\n"
        f"UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
    )
    await notify_admin(txt)
    await m.answer("Спасибо! Ваша заявка отправлена. Мы на связи.", reply_markup=main_kb())

# --- Заказ (контакт в один шаг) ---
@dp.callback_query(F.data == "go_order")
async def order_start(c: CallbackQuery, state: FSMContext):
    if not Store.accepting:
        return await c.answer("Приём заявок временно закрыт", show_alert=True)
    await state.set_state(Order.contact)
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, keyboard=[[
        KeyboardButton(text="Отправить мой номер", request_contact=True),
    ]])
    await c.message.answer("Оставьте телефон или напишите контакт (телеграм/почта):", reply_markup=kb)
    await c.answer()

@dp.message(Order.contact, F.contact)
async def order_contact_obj(m: Message, state: FSMContext):
    phone = re.sub(r"\D+", "", m.contact.phone_number or "")
    await finalize_order(m, state, phone=phone)

@dp.message(Order.contact)
async def order_contact_text(m: Message, state: FSMContext):
    phone = re.sub(r"\D+", "", (m.text or ""))
    await finalize_order(m, state, phone=phone, raw=m.text)

async def finalize_order(m: Message, state: FSMContext, phone: Optional[str], raw: Optional[str] = None):
    await state.clear()
    Store.stats["orders"] += 1
    clean = phone if (phone and 7 <= len(phone) <= 15) else (raw.strip() if raw else "—")
    await m.answer("Спасибо! Мы на связи. Возврат в меню…", reply_markup=ReplyKeyboardRemove())
    await m.answer("Главное меню:", reply_markup=main_kb())
    await notify_admin((
        "🛒 Заказ/контакт\n"
        f"От: {ufmt(m)}\n"
        f"Контакт: {esc(clean)}\n"
        f"UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
    ))

# --- Error handler (aiogram 3.7+: один аргумент event) ---
@dp.error()
async def on_error(event):
    exc = getattr(event, "exception", None)
    try:
        await notify_admin(f"⚠️ Ошибка: {html.escape(repr(exc))}")
    except Exception:
        pass
    logging.exception("Handler error: %s", exc)

# --- FastAPI app ---
app = FastAPI(title="Vimly — Client Demo Bot (WebApp)")

# Static for webapp
static_dir = os.path.join(os.path.dirname(__file__), "webapp")
if os.path.isdir(static_dir):
    app.mount("/webapp", StaticFiles(directory=static_dir), name="webapp")

@app.get("/", response_class=HTMLResponse)
async def index():
    return f"<h3>{esc(BRAND_NAME)} — {esc(BRAND_TAGLINE)}</h3>"

@app.get("/healthz", response_class=PlainTextResponse)
async def healthz():
    return "ok"
# ЯВНЫЙ роут на /webapp/quiz (работает и без слэша)
@app.get("/webapp/quiz", response_class=HTMLResponse)
async def webapp_quiz():
    index_path = os.path.join(static_dir, "quiz", "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, media_type="text/html")
    raise HTTPException(status_code=404, detail="webapp/quiz not found")

# Фавиконка, чтобы убрать 404 (можно вернуть hero как иконку)
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    hero = os.path.join(os.path.dirname(__file__), "assets", "hero.png")
    if os.path.exists(hero):
        return FileResponse(hero, media_type="image/png")
    return Response(status_code=204)

@app.post(WEBHOOK_PATH)
async def webhook(request: Request):
    if WEBHOOK_SECRET:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid secret token")
    data = await request.json()
    update = Update.model_validate(data)
    await dp.feed_update(bot, update)
    return {"ok": True}

# --- lifecycle ---
@app.on_event("startup")
async def on_startup():
    if MODE == "webhook":
        if BASE_URL:
            url = f"{BASE_URL}{WEBHOOK_PATH}"
            log.info("Setting webhook to: %r", url)
            try:
                await bot.set_webhook(
                    url=url,
                    secret_token=WEBHOOK_SECRET or None,
                    drop_pending_updates=True
                )
                log.info("Webhook set OK")
            except Exception as e:
                log.error("Failed to set webhook: %s", e)
        else:
            log.warning("BASE_URL is not set; webhook not configured")
    else:
        log.info("Polling mode — use __main__ launcher")

@app.on_event("shutdown")
async def on_shutdown():
    try:
        await bot.session.close()
    except Exception:
        pass

# ---- Local polling (dev) ----
if __name__ == "__main__":
    async def _run():
        log.info("Starting polling...")
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    asyncio.run(_run())
