# -*- coding: utf-8 -*-
"""
Vimly — Client Demo Bot
Стек: FastAPI + aiogram v3, один файл, без БД.
Режимы: webhook (Render) или polling (локально).

Меню:
• Процесс • Кейсы (демо) • Квиз‑заявка • Пакеты и цены • Заказать • Контакты • Бриф • Подарок

Фичи:
• Квиз (3 шага) и «Заказать» → заявка в админ‑чат
• Админ‑панель: вкл/выкл приёма, статистика, тест‑рассылка
• Отдача файла‑подарка (чек‑лист) из папки /assets
• Брендинг из ENV с дефолтом под "Vimly"
"""
import os, logging, re, asyncio
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, Update,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    FSInputFile
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

# ---- ENV ----
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN env var")

ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0") or "0")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/telegram/webhook/vimly")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
MODE = os.getenv("MODE", "webhook").lower()  # webhook | polling

# --- Branding defaults (can be overridden by ENV) ---
BRAND_NAME = os.getenv("BRAND_NAME", "Vimly").strip()
BRAND_TAGLINE = os.getenv("BRAND_TAGLINE", "Боты, которые продают").strip()
BRAND_TG = os.getenv("BRAND_TG", "@Vimly_bot").strip()
BRAND_SITE = os.getenv("BRAND_SITE", "").strip()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("vimly-demo")

bot = Bot(BOT_TOKEN, parse_mode="Markdown")
dp = Dispatcher()

# ---- STORE (in-memory demo) ----
class Store:
    accepting = True
    stats = {"starts": 0, "quiz": 0, "orders": 0}

# ---- FSM ----
class Quiz(StatesGroup):
    niche = State()
    goal = State()
    deadline = State()

class Order(StatesGroup):
    contact = State()

# ---- UI ----
def main_kb() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="🧭 Процесс", callback_data="go_process"),
            InlineKeyboardButton(text="💼 Кейсы (демо)", callback_data="go_cases"),
        ],
        [
            InlineKeyboardButton(text="🧪 Квиз‑заявка", callback_data="go_quiz"),
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
        [
            InlineKeyboardButton(text="🛠 Админ", callback_data="admin_open"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_kb() -> InlineKeyboardMarkup:
    on = "🟢" if Store.accepting else "🔴"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"{on} Приём заявок", callback_data="admin_toggle"),
            InlineKeyboardButton(text="📈 Статистика", callback_data="admin_stats"),
        ],
        [
            InlineKeyboardButton(text="📣 Тест‑рассылка", callback_data="admin_broadcast"),
            InlineKeyboardButton(text="⬅️ Меню", callback_data="go_menu"),
        ]
    ])

# ---- HELPERS ----
def header() -> str:
    parts = [f"*{BRAND_NAME}*", BRAND_TAGLINE]
    if BRAND_SITE:
        parts.append(BRAND_SITE)
    return "\n".join(parts)

def ufmt(m: Message) -> str:
    user = m.from_user
    tag = f"@{user.username}" if user.username else f"id={user.id}"
    return f"{user.full_name} ({tag})"

def sanitize_phone(s: str) -> Optional[str]:
    import re as _re
    digits = _re.sub(r"\D+", "", s or "")
    return digits if 7 <= len(digits) <= 15 else None

async def notify_admin(text: str):
    if not ADMIN_CHAT_ID:
        return
    try:
        await bot.send_message(ADMIN_CHAT_ID, text, disable_notification=True)
    except Exception as e:
        log.warning("notify_admin failed: %s", e)

# ---- HANDLERS ----
@dp.message(CommandStart())
async def on_start(m: Message):
    Store.stats["starts"] += 1
    welcome = (
        f"{header()}\n\n"
        "Этот бот — *демо для клиентов*: меню, кейсы, квиз и запись в 2 клика.\n"
        "Нажмите кнопку ниже 👇"
    )
    await m.answer(welcome, reply_markup=main_kb())

@dp.message(Command("menu"))
async def on_menu(m: Message):
    await m.answer("Главное меню:", reply_markup=main_kb())

@dp.message(Command("admin"))
async def on_admin(m: Message):
    if m.from_user.id != ADMIN_CHAT_ID:
        return await m.answer("Админ‑панель доступна владельцу бота.")
    await m.answer("Админ‑панель:", reply_markup=admin_kb())

# --- Callbacks: меню ---
@dp.callback_query(F.data == "go_menu")
async def cb_menu(c: CallbackQuery):
    await c.message.edit_text("Главное меню:", reply_markup=main_kb())
    await c.answer()

@dp.callback_query(F.data == "go_process")
async def cb_process(c: CallbackQuery):
    txt = (
        "Как запускаем за 1–3 дня:\n"
        "1) *Созвон 15 минут* — фиксируем цели\n"
        "2) *MVP* — меню + квиз + админ‑чат\n"
        "3) *Запуск* — подключаем Sheets/оплату/канал\n"
        "4) *Поддержка* — рассылки, правки, отчёты\n\n"
        "Сроки и бюджет фиксируем письменно."
    )
    await c.message.edit_text(txt, reply_markup=main_kb())
    await c.answer()

@dp.callback_query(F.data == "go_cases")
async def cb_cases(c: CallbackQuery):
    txt = (
        "Кейсы (демо):\n"
        "• Барбершоп — запись и отзывы, 2 экрана, +26 заявок/мес\n"
        "• Пекарня — квиз + купоны, ~18% конверсия в визит\n"
        "• Автор‑канал — оплата → доступ в закрытый чат\n"
        "• Коворкинг — афиша/RSVP, считает гостей и выгружает список\n\n"
        "Покажу живые прототипы на созвоне."
    )
    await c.message.edit_text(txt, reply_markup=main_kb())
    await c.answer()

@dp.callback_query(F.data == "go_prices")
async def cb_prices(c: CallbackQuery):
    txt = (
        "*Пакеты и цены:*\n\n"
        "• *Lite* — 15–20k ₽: меню/квиз/заявки, без БД и оплаты\n"
        "• *Standard* — 25–45k ₽: + Google Sheets, админ‑панель, напоминания\n"
        "• *Pro* — 50–90k ₽: + оплата, доступ в канал, логи, бэкапы\n\n"
        "_Поддержка 3–10k ₽/мес_: правки, рассылки, мониторинг"
    )
    await c.message.edit_text(txt, reply_markup=main_kb())
    await c.answer()

@dp.callback_query(F.data == "go_contacts")
async def cb_contacts(c: CallbackQuery):
    txt = (
        "*Контакты:*\n"
        f"Telegram: {BRAND_TG}\n"
        f"Сайт/портфолио: {BRAND_SITE or '—'}\n\n"
        "Оставьте телефон — свяжемся в удобное время."
    )
    await c.message.edit_text(txt, reply_markup=main_kb())
    await c.answer()

@dp.callback_query(F.data == "go_brief")
async def cb_brief(c: CallbackQuery):
    brief = (
        "*Мини‑бриф (7 вопросов):*\n"
        "1) Ниша и город\n"
        "2) Цель бота (заявки/запись/оплата/отзывы)\n"
        "3) Кнопки меню (4–6)\n"
        "4) Что слать в админ‑чат (лиды/фото/файлы)\n"
        "5) Нужны ли Google Sheets и рассылки\n"
        "6) Нужна ли оплата и доступ в канал\n"
        "7) Срок запуска и бюджет"
    )
    await c.message.edit_text(brief, reply_markup=main_kb())
    await c.answer()

@dp.callback_query(F.data == "go_gift")
async def cb_gift(c: CallbackQuery):
    path = os.path.join(os.path.dirname(__file__), "assets", "checklist-7-screens.txt")
    try:
        await bot.send_document(c.from_user.id, FSInputFile(path), caption="🎁 Чек‑лист: 7 экранов демо‑бота, которые продают")
        await c.answer("Отправил подарок в личку.")
    except Exception:
        await c.answer("Не удалось отправить файл. Напишите в личку.", show_alert=True)

# --- Квиз ---
@dp.callback_query(F.data == "go_quiz")
async def quiz_start(c: CallbackQuery, state: FSMContext):
    if not Store.accepting:
        return await c.answer("Приём заявок временно закрыт", show_alert=True)
    await state.set_state(Quiz.niche)
    await c.message.edit_text("🧪 Квиз: ваша ниша и город? (1/3)")
    await c.answer()

@dp.message(Quiz.niche)
async def quiz_niche(m: Message, state: FSMContext):
    await state.update_data(niche=m.text.strip()[:120])
    await state.set_state(Quiz.goal)
    await m.answer("Цель бота? (2/3) — заявки, запись, оплата, отзывы…")

@dp.message(Quiz.goal)
async def quiz_goal(m: Message, state: FSMContext):
    await state.update_data(goal=m.text.strip()[:180])
    await state.set_state(Quiz.deadline)
    await m.answer("Срок запуска? (3/3) — например: 2–3 дня / дата")

@dp.message(Quiz.deadline)
async def quiz_done(m: Message, state: FSMContext):
    data = await state.update_data(deadline=m.text.strip()[:100])
    await state.clear()
    Store.stats["quiz"] += 1

    user_text = (
        "Спасибо! Заявка получена 🎉\n\n"
        f"Ниша: {data.get('niche')}\n"
        f"Цель: {data.get('goal')}\n"
        f"Срок: {data.get('deadline')}\n\n"
        "Свяжемся в ближайшее время."
    )
    await m.answer(user_text, reply_markup=main_kb())

    at = (
        "🆕 Заявка (квиз)\n"
        f"От: {ufmt(m)}\n"
        f"Ниша: {data.get('niche')}\n"
        f"Цель: {data.get('goal')}\n"
        f"Срок: {data.get('deadline')}\n"
        f"UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
    )
    await notify_admin(at)

# --- Заказ ---
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
    phone = sanitize_phone(m.contact.phone_number)
    await finalize_order(m, state, phone=phone)

@dp.message(Order.contact)
async def order_contact_text(m: Message, state: FSMContext):
    phone = sanitize_phone(m.text)
    await finalize_order(m, state, phone=phone, raw=m.text)

async def finalize_order(m: Message, state: FSMContext, phone: Optional[str], raw: Optional[str]=None):
    await state.clear()
    Store.stats["orders"] += 1
    clean = phone or (raw.strip() if raw else "—")
    await m.answer("Спасибо! Мы на связи. Возврат в меню…", reply_markup=ReplyKeyboardRemove())
    await m.answer("Главное меню:", reply_markup=main_kb())
    at = (
        "🛒 Заказ/контакт\n"
        f"От: {ufmt(m)}\n"
        f"Контакт: {clean}\n"
        f"UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
    )
    await notify_admin(at)

# --- Админ ---
@dp.callback_query(F.data == "admin_open")
async def admin_open(c: CallbackQuery):
    if c.from_user.id != ADMIN_CHAT_ID:
        return await c.answer("Доступ только владельцу", show_alert=True)
    await c.message.edit_text("Админ‑панель:", reply_markup=admin_kb())
    await c.answer()

@dp.callback_query(F.data == "admin_toggle")
async def admin_toggle(c: CallbackQuery):
    if c.from_user.id != ADMIN_CHAT_ID:
        return await c.answer("Нет доступа", show_alert=True)
    Store.accepting = not Store.accepting
    await c.message.edit_text("Админ‑панель:", reply_markup=admin_kb())
    await c.answer("Режим приёма: " + ("включён" if Store.accepting else "выключен"))

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(c: CallbackQuery):
    if c.from_user.id != ADMIN_CHAT_ID:
        return await c.answer("Нет доступа", show_alert=True)
    s = Store.stats
    txt = f"Статистика:\n/starts: {s['starts']}\n/quiz: {s['quiz']}\n/orders: {s['orders']}"
    await c.message.edit_text(txt, reply_markup=admin_kb())
    await c.answer()

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(c: CallbackQuery):
    if c.from_user.id != ADMIN_CHAT_ID:
        return await c.answer("Нет доступа", show_alert=True)
    await notify_admin("📣 Тест‑рассылка: сервисное сообщение для владельца бота.")
    await c.answer("Отправил тест‑сообщение в ваш личный чат.")

# --- Errors ---
@dp.error()
async def on_error(event, exception):
    try:
        await notify_admin(f"⚠️ Ошибка: {exception}")
    except Exception:
        pass
    logging.exception("Handler error: %s", exception)

# ---- FastAPI / webhook ----
app = FastAPI(title="Vimly — Client Demo Bot")

@app.get("/", response_class=HTMLResponse)
async def index():
    return f"<h3>{BRAND_NAME} — {BRAND_TAGLINE}</h3>"

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

@app.on_event("startup")
async def on_startup():
    if MODE == "webhook":
        if BASE_URL:
            url = f"{BASE_URL}{WEBHOOK_PATH}"
            await bot.set_webhook(url=url, secret_token=WEBHOOK_SECRET or None, drop_pending_updates=True)
            log.info("Webhook set: %s", url)
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

# ---- Local polling (for dev) ----
if __name__ == "__main__":
    async def _run():
        log.info("Starting polling...")
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    asyncio.run(_run())
