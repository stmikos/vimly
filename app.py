# -*- coding: utf-8 -*-
"""
Vimly — Client Demo Bot (FastAPI + aiogram 3.7+)

Что внутри:
- Квиз:
  • в ЛС: WebApp («🧪 Квиз (в Telegram)») + «🌐 Квиз (в браузере)»
  • в группах: «🧪 Квиз (в чате)» через ForceReply (обходит privacy)
- Контакты: «✍️ Написать админу»
  • в группах — диплинк в ЛС, в ЛС — сбор сообщения
  • антиспам: не чаще одного сообщения админу в N секунд (ENV ADMIN_DM_COOLDOWN_SEC)
- Админка (только ADMIN_CHAT_ID): уникальные пользователи, счётчики
- Лиды: сначала хедер в группу, затем полная карточка; HTTP-fallback /webapp/submit
- Убран «Бриф»
"""

import os, logging, re, asyncio, json, html, secrets
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Response, Body
from fastapi.responses import HTMLResponse, PlainTextResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, Update,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    ForceReply, FSInputFile,
)
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

# ---------- ENV ----------
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

# Лид-чат: -100… (супергруппа/канал) или @username канала
LEADS_RAW = (os.getenv("LEADS_CHAT_ID") or "").strip()
LEADS_THREAD_ID = int((os.getenv("LEADS_THREAD_ID") or "0").strip() or "0")

BASE_URL = _norm_base_url(os.getenv("BASE_URL"))
WEBHOOK_PATH = _norm_path(os.getenv("WEBHOOK_PATH") or "/telegram/webhook/vimly")
WEBHOOK_SECRET = (os.getenv("WEBHOOK_SECRET") or "").strip()
MODE = (os.getenv("MODE") or "webhook").strip().lower()  # webhook | polling

# Антиспам на ЛС админу
ADMIN_DM_COOLDOWN_SEC = int((os.getenv("ADMIN_DM_COOLDOWN_SEC") or "60").strip() or "60")

# ---------- BRAND ----------
BRAND_NAME = (os.getenv("BRAND_NAME") or "Vimly").strip()
BRAND_TAGLINE = (os.getenv("BRAND_TAGLINE") or "Боты, которые продают").strip()
BRAND_TG = (os.getenv("BRAND_TG") or "@Vimly_bot").strip()
BRAND_SITE = (os.getenv("BRAND_SITE") or "").strip()

# ---------- LOG ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("vimly-webapp-demo")
log.info("Leads target (raw): %r  thread: %s", LEADS_RAW, LEADS_THREAD_ID or "—")

# ---------- AIOGRAM ----------
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ---------- STORE ----------
class Store:
    accepting = True
    started_at = datetime.now(timezone.utc)
    users = set()  # уникальные пользователи по /start
    stats = {"starts": 0, "quiz": 0, "orders": 0, "webquiz": 0, "contact_msgs": 0}
Store.promos = {}
Store.gift_claimed = set()
Store.last_admin_dm = {}  # {user_id: datetime_utc}

# ---------- FSM ----------
class Quiz(StatesGroup):
    niche = State()
    goal = State()
    deadline = State()

class Order(StatesGroup):
    contact = State()

class AdminMsg(StatesGroup):
    text = State()

# ---------- HELPERS ----------
def esc(s: Optional[str]) -> str:
    return html.escape(s or "", quote=False)

def header() -> str:
    parts = [f"<b>{esc(BRAND_NAME)}</b>", esc(BRAND_TAGLINE)]
    if BRAND_SITE:
        parts.append(esc(BRAND_SITE))
    return "\n".join(parts)

def ufmt(m: Message) -> str:
    u = m.from_user
    tag = f"@{u.username}" if u.username else f"id={u.id}"
    return esc(f"{u.full_name} ({tag})")

def parse_leads_target(s: str):
    s = (s or "").strip()
    if not s: return None
    if s.startswith("@"): return s
    try: return int(s)
    except ValueError: return None

async def _send_to_leads(text: str) -> bool:
    """Постит в лид-чат. True — доставлено, False — ошибка (алерт админу внутри)."""
    target = parse_leads_target(LEADS_RAW)
    if not target:
        log.error("LEADS_CHAT_ID is empty or invalid: %r", LEADS_RAW)
        return False
    try:
        kwargs = {"disable_web_page_preview": True}
        if LEADS_THREAD_ID:
            kwargs["message_thread_id"] = LEADS_THREAD_ID
        msg = await bot.send_message(target, text, **kwargs)
        log.info("Lead → chat=%s thread=%s msg_id=%s", LEADS_RAW, LEADS_THREAD_ID or "—", getattr(msg, "message_id", "—"))
        return True
    except Exception as e:
        log.warning("notify_leads failed (%r): %s", LEADS_RAW, e)
        if ADMIN_CHAT_ID:
            try:
                await bot.send_message(
                    ADMIN_CHAT_ID,
                    f"⚠️ Не удалось отправить в лид-чат {LEADS_RAW!r}:\n<code>{esc(str(e))}</code>",
                    disable_web_page_preview=True
                )
            except Exception:
                pass
        return False

async def notify_admin(text: str) -> bool:
    if ADMIN_CHAT_ID:
        try:
            await bot.send_message(ADMIN_CHAT_ID, text, disable_notification=True, disable_web_page_preview=True)
        except Exception as e:
            log.warning("notify_admin failed: %s", e)
    return await _send_to_leads(text)

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_CHAT_ID and ADMIN_CHAT_ID != 0

async def safe_edit(c: CallbackQuery, html_text: str, kb: Optional[InlineKeyboardMarkup] = None):
    if kb is None:
        kb = main_kb(is_private=(c.message.chat.type == "private"), is_admin=is_admin(c.from_user.id))
    m = c.message
    try:
        if getattr(m, "content_type", None) in {"photo","video","animation","document","audio","voice","video_note"}:
            await m.edit_caption(caption=html_text, reply_markup=kb)
        else:
            await m.edit_text(html_text, reply_markup=kb)
    except TelegramBadRequest:
        await m.answer(html_text, reply_markup=kb)

def sanitize_phone(s: str) -> Optional[str]:
    digits = re.sub(r"\D+", "", s or "")
    return digits if 7 <= len(digits) <= 15 else None

def gen_promo_for(user_id: int) -> dict:
    suffix = secrets.token_hex(2).upper()
    code = f"VIM-{str(user_id)[-4:]}-{suffix}"
    expires = datetime.now(timezone.utc) + timedelta(hours=72)
    data = {"code": code, "expires_utc": expires.strftime("%Y-%m-%d %H:%M UTC")}
    Store.promos[user_id] = data
    return data

def admin_dm_left(user_id: int) -> int:
    """Сколько секунд осталось до следующего сообщения админу от этого пользователя."""
    ts = Store.last_admin_dm.get(user_id)
    if not ts:
        return 0
    delta = (datetime.now(timezone.utc) - ts).total_seconds()
    left = int(ADMIN_DM_COOLDOWN_SEC - delta)
    return max(0, left)

def admin_dm_mark(user_id: int):
    Store.last_admin_dm[user_id] = datetime.now(timezone.utc)

BOT_USERNAME = ""  # будет установлен на старте

def deep_link(suffix: str) -> str:
    # https://t.me/<bot>?start=<suffix>
    su = (suffix or "").strip().replace(" ", "_")
    return f"https://t.me/{BOT_USERNAME}?start={su}" if BOT_USERNAME else ""

def force_reply_if_needed(chat_type: str, placeholder: str) -> Optional[ForceReply]:
    return ForceReply(selective=True, input_field_placeholder=placeholder) if chat_type != "private" else None

MAX_TG = 3900  # запас под лимит 4096
def build_lead(kind: str, m: Message, company: str, task: str, contact: str) -> str:
    base = f"🧪 Заявка ({kind})\nОт: {ufmt(m)}\n"
    comp = (company or "").strip()
    tsk  = (task or "").strip()
    cnt  = (contact or "").strip()
    body = (
        f"Компания: {esc(comp) or '—'}\n"
        f"Задача: {esc(tsk) or '—'}\n"
        f"Контакт: {esc(cnt) or '—'}\n"
        f"UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
    )
    txt = base + body
    if len(txt) <= MAX_TG: return txt

    comp_max = max(150, int((MAX_TG - len(base) - 100) * 0.45))
    tsk_max  = max(150, int((MAX_TG - len(base) - 100) * 0.45))
    def cut(s: str, n: int) -> str:
        s = s.strip()
        return (s[: n-1] + "…") if len(s) > n else s
    comp2 = cut(comp, comp_max)
    tsk2  = cut(tsk,  tsk_max)
    txt2 = base + (
        f"Компания: {esc(comp2) or '—'}\n"
        f"Задача: {esc(tsk2) or '—'}\n"
        f"Контакт: {esc(cnt) or '—'}\n"
        f"(обрезано)\n"
        f"UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
    )
    if len(txt2) > MAX_TG:
        tsk2 = cut(tsk2, max(120, tsk_max - (len(txt2) - MAX_TG + 20)))
        txt2 = base + (
            f"Компания: {esc(comp2) or '—'}\n"
            f"Задача: {esc(tsk2) or '—'}\n"
            f"Контакт: {esc(cnt) or '—'}\n"
            f"(обрезано)\n"
            f"UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
        )
    return txt2

async def send_lead_header(kind: str, m: Message) -> bool:
    head = f"📥 Новая заявка ({kind})\nОт: {ufmt(m)}"
    return await _send_to_leads(head)

# ---------- UI ----------
def main_kb(is_private: bool, is_admin: bool) -> InlineKeyboardMarkup:
    # Квиз: WebApp в ЛС / чат-квиз в группах + браузерный fallback
    webapp_btn = (
        InlineKeyboardButton(
            text="🧪 Квиз (в Telegram)",
            web_app=WebAppInfo(url=f"{BASE_URL}/webapp/quiz/")
        ) if (BASE_URL and is_private) else
        InlineKeyboardButton(text="🧪 Квиз (в чате)", callback_data="go_quiz")
    )
    browser_btn = InlineKeyboardButton(
        text="🌐 Квиз (в браузере)", url=f"{BASE_URL}/webapp/quiz/"
    ) if BASE_URL else None
    row_quiz = [webapp_btn] + ([browser_btn] if browser_btn else [])

    rows = [
        [InlineKeyboardButton(text="🧭 Процесс", callback_data="go_process"),
         InlineKeyboardButton(text="💼 Кейсы (демо)", callback_data="go_cases")],
        row_quiz,
        [InlineKeyboardButton(text="💸 Пакеты и цены", callback_data="go_prices"),
         InlineKeyboardButton(text="🛒 Заказать", callback_data="go_order")],
        [InlineKeyboardButton(text="📬 Контакты", callback_data="go_contacts"),
         InlineKeyboardButton(text="🎁 Подарок", callback_data="go_gift")],
        [InlineKeyboardButton(text="↘ Скрыть меню", callback_data="hide_menu")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="🛠 Админ", callback_data="admin_open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ---------- HANDLERS ----------
@dp.message(CommandStart())
async def on_start(m: Message, state: FSMContext):
    Store.stats["starts"] += 1
    Store.users.add(m.from_user.id)

    # диплинк-параметр
    parts = (m.text or "").split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""

    hero = os.path.join(os.path.dirname(__file__), "assets", "hero.png")
    try:
        await m.answer_photo(FSInputFile(hero), caption=header())
    except Exception:
        await m.answer(header())

    # спец-режимы
    if arg == "contact":
        await state.set_state(AdminMsg.text)
        kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, keyboard=[[KeyboardButton(text="Отмена")]])
        note = f"(антиспам: не чаще чем раз в {ADMIN_DM_COOLDOWN_SEC} сек)"
        await m.answer("Напишите сообщение админу. Можно текст/медиа.\n" + note, reply_markup=kb)
        return

    if arg == "quiz":
        await state.set_state(Quiz.niche)
        kb = ForceReply(selective=True, input_field_placeholder="Ниша и город")
        await m.answer("🧪 Квиз: ваша ниша и город? (1/3)", reply_markup=kb)
        return

    await m.answer(
        "Демо-бот: квиз, кейсы, запись. Нажмите кнопку ниже 👇",
        reply_markup=main_kb(is_private=(m.chat.type == "private"), is_admin=is_admin(m.from_user.id))
    )

@dp.message(Command("menu"))
async def on_menu(m: Message):
    await m.answer("Главное меню:", reply_markup=main_kb(is_private=(m.chat.type == "private"), is_admin=is_admin(m.from_user.id)))

@dp.message(Command("chatid"))
async def cmd_chatid(m: Message):
    await m.answer(f"chat_id: <code>{m.chat.id}</code>")

@dp.message(Command("threadid"))
async def cmd_threadid(m: Message):
    tid = getattr(m, "message_thread_id", None)
    await m.answer(f"thread_id: <code>{tid}</code>")

@dp.channel_post(Command("chatid"))
async def channel_chatid(m: Message):
    await m.answer(f"chat_id: <code>{m.chat.id}</code>")

@dp.channel_post(Command("threadid"))
async def channel_threadid(m: Message):
    tid = getattr(m, "message_thread_id", None)
    await m.answer(f"thread_id: <code>{tid}</code>")

# --- Диагностика лид-чата ---
@dp.message(Command("check_leads"))
async def check_leads(m: Message):
    target_raw = LEADS_RAW
    def _parse(s: str):
        s = (s or "").strip()
        if not s: return None
        if s.startswith("@"): return s
        try: return int(s)
        except ValueError: return None
    target = _parse(target_raw)
    if not target:
        return await m.answer("LEADS_CHAT_ID не задан или некорректен.")
    try:
        me = await bot.get_me()
        chat = await bot.get_chat(target)
        member = await bot.get_chat_member(chat.id, me.id)
        def g(obj, attr, default="—"): return getattr(obj, attr, default)
        info = (
            "📊 Лид-чат найден:\n"
            f"• chat.id: <code>{chat.id}</code>\n"
            f"• type: {g(chat, 'type')}\n"
            f"• title: {g(chat, 'title')}\n"
            f"• is_forum: {g(chat, 'is_forum', False)}\n"
            f"• бот в чате как: {g(member, 'status')}\n"
            f"• can_send_messages: {g(member, 'can_send_messages', '—')}\n"
            f"• can_post_messages (для каналов): {g(member, 'can_post_messages', '—')}\n"
        )
        await m.answer(info)
    except Exception as e:
        await m.answer(f"❌ Не удалось прочитать чат {target_raw!r}:\n<code>{e}</code>")

@dp.message(Command("test_leads"))
async def test_leads_cmd(m: Message):
    if not is_admin(m.from_user.id): return
    def _parse(s: str):
        s = (s or "").strip()
        if not s: return None
        if s.startswith("@"): return s
        try: return int(s)
        except ValueError: return None
    target = _parse(LEADS_RAW)
    if not target:
        return await m.answer("LEADS_CHAT_ID не задан или некорректен.")
    try:
        kwargs = {}
        if LEADS_THREAD_ID:
            kwargs["message_thread_id"] = LEADS_THREAD_ID
        await bot.send_message(target, "🔔 Тест в чат лидов: работает ✅", **kwargs)
        await m.answer(f"OK → {LEADS_RAW!r} (thread={LEADS_THREAD_ID or '—'})")
    except Exception as e:
        await m.answer(f"❌ Не отправилось в {LEADS_RAW!r}:\n<code>{e}</code>")

# --- Меню / контент ---
@dp.callback_query(F.data == "hide_menu")
async def cb_hide_menu(c: CallbackQuery):
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    try:
        await c.message.edit_text("Меню скрыто. Напишите /menu чтобы открыть.")
    except TelegramBadRequest:
        await c.message.answer("Меню скрыто. Напишите /menu чтобы открыть.")
    await c.answer()

@dp.callback_query(F.data == "go_menu")
async def cb_menu(c: CallbackQuery):
    await safe_edit(c, "Главное меню:")

@dp.callback_query(F.data == "go_process")
async def cb_process(c: CallbackQuery):
    txt = (
        "Как запускаем за 1–3 дня:\n"
        "1) <b>Созвон 15 минут</b> — фиксируем цели\n"
        "2) <b>MVP</b> — меню + квиз + админ-чат\n"
        "3) <b>Запуск</b> — подключаем Sheets/оплату/канал\n"
        "4) <b>Поддержка</b> — рассылки, правки, отчёты"
    )
    await safe_edit(c, txt); await c.answer()

@dp.callback_query(F.data == "go_cases")
async def cb_cases(c: CallbackQuery):
    txt = (
        "Кейсы (демо):\n"
        "• Барбершоп — запись и отзывы\n"
        "• Пекарня — квиз + купоны\n"
        "• Автор-канал — оплата → доступ\n"
        "• Коворкинг — афиша/RSVP"
    )
    await safe_edit(c, txt); await c.answer()

@dp.callback_query(F.data == "go_prices")
async def cb_prices(c: CallbackQuery):
    txt = (
        "<b>Пакеты и цены:</b>\n\n"
        "• <b>Lite</b> — 15–20k ₽\n"
        "• <b>Standard</b> — 25–45k ₽\n"
        "• <b>Pro</b> — 50–90k ₽\n\n"
        "<i>Поддержка 3–10k ₽/мес</i>"
    )
    await safe_edit(c, txt); await c.answer()

# --- Контакты + «написать админу» ---
@dp.callback_query(F.data == "go_contacts")
async def cb_contacts(c: CallbackQuery, state: FSMContext):
    # в группах — диплинк в ЛС
    if c.message.chat.type != "private":
        url = deep_link("contact")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Открыть диалог с админом", url=url)],
            [InlineKeyboardButton(text="⬅️ Меню", callback_data="go_menu")],
        ])
        txt = "<b>Контакты</b>\nНажмите кнопку, чтобы написать админу в ЛС."
        await safe_edit(c, txt, kb); await c.answer(); return

    # в ЛС — запускаем сбор сообщения
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Меню", callback_data="go_menu")],
    ])
    note = f"(антиспам: не чаще {ADMIN_DM_COOLDOWN_SEC} сек)"
    await safe_edit(c, f"<b>Напишите сообщение админу.</b>\n{note}", kb)
    await c.message.answer(
        "Пришлите текст/медиа. «Отмена» — выйти.",
        reply_markup=ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, keyboard=[[KeyboardButton(text="Отмена")]])
    )
    await state.set_state(AdminMsg.text)
    await c.answer()

@dp.callback_query(F.data == "admin_open")
async def cb_admin_open(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Только для владельца бота", show_alert=True); return
    uptime = datetime.now(timezone.utc) - Store.started_at
    txt = (
        "<b>🛠 Админ-панель</b>\n"
        f"Uptime: {str(uptime).split('.',1)[0]}\n"
        f"Уникальных пользователей: <b>{len(Store.users)}</b>\n"
        f"Starts: {Store.stats['starts']}\n"
        f"WebQuiz: {Store.stats['webquiz']}\n"
        f"ChatQuiz: {Store.stats['quiz']}\n"
        f"Orders: {Store.stats['orders']}\n"
        f"Msgs→Admin: {Store.stats['contact_msgs']}\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📈 Обновить", callback_data="admin_open")],
        [InlineKeyboardButton(text="⬅️ Меню", callback_data="go_menu")]
    ])
    await safe_edit(c, txt, kb); await c.answer()

# --- Сообщение админу (FSM) ---
@dp.message(AdminMsg.text, F.text.casefold() == "отмена")
async def contact_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменено.", reply_markup=ReplyKeyboardRemove())
    await m.answer("Главное меню:", reply_markup=main_kb(is_private=(m.chat.type == "private"), is_admin=is_admin(m.from_user.id)))

@dp.message(AdminMsg.text, F.text)
async def contact_text(m: Message, state: FSMContext):
    # антиспам
    left = admin_dm_left(m.from_user.id)
    if left > 0:
        await m.answer(f"Антиспам: подождите ещё {left} сек перед следующим сообщением админу 🙂")
        return
    Store.stats["contact_msgs"] += 1
    if ADMIN_CHAT_ID:
        txt = f"✉️ Сообщение админу от {ufmt(m)}:\n\n{esc(m.text)}"
        await bot.send_message(ADMIN_CHAT_ID, txt, disable_web_page_preview=True)
        admin_dm_mark(m.from_user.id)
    await state.clear()
    await m.answer("Сообщение отправлено админу. Спасибо!", reply_markup=ReplyKeyboardRemove())
    await m.answer("Главное меню:", reply_markup=main_kb(is_private=(m.chat.type == "private"), is_admin=is_admin(m.from_user.id)))

@dp.message(AdminMsg.text)
async def contact_any(m: Message, state: FSMContext):
    # антиспам
    left = admin_dm_left(m.from_user.id)
    if left > 0:
        await m.answer(f"Антиспам: подождите ещё {left} сек перед следующим сообщением админу 🙂")
        return
    Store.stats["contact_msgs"] += 1
    if ADMIN_CHAT_ID:
        head = f"✉️ Сообщение админу от {ufmt(m)} (медиа ниже)"
        await bot.send_message(ADMIN_CHAT_ID, head)
        try:
            await bot.copy_message(ADMIN_CHAT_ID, from_chat_id=m.chat.id, message_id=m.message_id)
        except Exception as e:
            await bot.send_message(ADMIN_CHAT_ID, f"(не удалось скопировать медиа)\n<code>{esc(str(e))}</code>")
        admin_dm_mark(m.from_user.id)
    await state.clear()
    await m.answer("Сообщение отправлено админу. Спасибо!", reply_markup=ReplyKeyboardRemove())
    await m.answer("Главное меню:", reply_markup=main_kb(is_private=(m.chat.type == "private"), is_admin=is_admin(m.from_user.id)))

# --- 🎁 Подарок ---
@dp.callback_query(F.data == "go_gift")
async def cb_gift(c: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Чек-лист PDF", callback_data="gift_pdf"),
         InlineKeyboardButton(text="🎟 Промокод −20% (72ч)", callback_data="gift_promo")],
        [InlineKeyboardButton(text="⬅️ Меню", callback_data="go_menu")]
    ])
    txt = "<b>Выберите подарок</b>: чек-лист PDF или промокод −20% на Lite (72ч)."
    await safe_edit(c, txt, kb); await c.answer()

@dp.callback_query(F.data == "gift_pdf")
async def cb_gift_pdf(c: CallbackQuery):
    uid = c.from_user.id
    pdf_path = os.path.join(os.path.dirname(__file__), "assets", "gifts", "checklist.pdf")
    caption = (
        "<b>Чек-лист: «Бот, который окупится за 48 часов»</b>\n"
        "Цель • Меню • УТП • Квиз • Лиды • Автоответ • Оффер • Кейсы • Память • Правки • Рассылки • Цифры"
    )
    try:
        if os.path.exists(pdf_path):
            await c.message.answer_document(FSInputFile(pdf_path), caption=caption)
        else:
            await c.message.answer(caption)
        Store.gift_claimed.add(uid)
        await notify_admin(f"🎁 PDF чек-лист выдан: {c.from_user.full_name} (@{c.from_user.username or '—'})")
    except Exception as e:
        await c.message.answer(f"Не удалось отправить PDF: <code>{esc(str(e))}</code>")
    await c.answer()

@dp.callback_query(F.data == "gift_promo")
async def cb_gift_promo(c: CallbackQuery):
    uid = c.from_user.id
    promo = Store.promos.get(uid) or gen_promo_for(uid)
    txt = (
        "<b>Ваш промокод: </b><code>{code}</code>\n"
        "Скидка: −20% на Lite, до {exp}\n"
        "Примените при подтверждении заказа."
    ).format(code=esc(promo["code"]), exp=esc(promo["expires_utc"]))
    await c.message.answer(txt)
    await notify_admin(f"🎟 Промокод выдан: {c.from_user.full_name} → {promo['code']} (до {promo['expires_utc']})")
    Store.gift_claimed.add(uid)
    await c.answer()

# --- Заказ (контакт) ---
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

async def finalize_order(m: Message, state: FSMContext, phone: Optional[str], raw: Optional[str] = None):
    await state.clear()
    Store.stats["orders"] += 1
    clean = phone or (raw.strip() if raw else "—")
    msg = (
        "🛒 Заказ/контакт\n"
        f"От: {ufmt(m)}\n"
        f"Контакт: {esc(clean)}\n"
        f"UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
    )
    await m.answer("Спасибо! Мы на связи.", reply_markup=ReplyKeyboardRemove())
    await m.answer("Главное меню:", reply_markup=main_kb(is_private=(m.chat.type == "private"), is_admin=is_admin(m.from_user.id)))
    await notify_admin(msg)

# --- Fallback чат-квиз (через ForceReply в группах) ---
@dp.callback_query(F.data == "go_quiz")
async def quiz_start(c: CallbackQuery, state: FSMContext):
    if not Store.accepting:
        return await c.answer("Приём заявок временно закрыт", show_alert=True)
    await state.set_state(Quiz.niche)
    kb = force_reply_if_needed(c.message.chat.type, "Ниша и город")
    await safe_edit(c, "🧪 Квиз: ваша ниша и город? (1/3)", kb=None)
    await c.message.answer("Ответьте на это сообщение:", reply_markup=kb)
    await c.answer()

@dp.message(Quiz.niche)
async def quiz_niche(m: Message, state: FSMContext):
    await state.update_data(niche=(m.text or "").strip()[:200])
    await state.set_state(Quiz.goal)
    kb = force_reply_if_needed(m.chat.type, "Цель бота")
    if kb:
        await m.answer("Цель бота? (2/3) — заявки, запись, оплата, отзывы…\nОтветьте на это сообщение:", reply_markup=kb)
    else:
        await m.answer("Цель бота? (2/3) — заявки, запись, оплата, отзывы…")

@dp.message(Quiz.goal)
async def quiz_goal(m: Message, state: FSMContext):
    await state.update_data(goal=(m.text or "").strip()[:300])
    await state.set_state(Quiz.deadline)
    kb = force_reply_if_needed(m.chat.type, "Срок запуска")
    if kb:
        await m.answer("Срок запуска? (3/3) — например: 2–3 дня / дата\nОтветьте на это сообщение:", reply_markup=kb)
    else:
        await m.answer("Срок запуска? (3/3) — например: 2–3 дня / дата")

@dp.message(Quiz.deadline)
async def quiz_done(m: Message, state: FSMContext):
    data = await state.update_data(deadline=(m.text or "").strip()[:100])
    await state.clear()
    Store.stats["quiz"] += 1
    msg = (
        "🆕 Заявка (квиз-чат)\n"
        f"От: {ufmt(m)}\n"
        f"Ниша: {esc(data.get('niche'))}\n"
        f"Цель: {esc(data.get('goal'))}\n"
        f"Срок: {esc(data.get('deadline'))}\n"
        f"UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
    )
    await m.answer("Ваша анкета отправлена, спасибо! ✅", reply_markup=main_kb(is_private=(m.chat.type == "private"), is_admin=is_admin(m.from_user.id)))
    await notify_admin(msg)

# --- Приём данных из WebApp ---
@dp.message(F.web_app_data)
async def on_webapp_data(m: Message):
    Store.stats["webquiz"] += 1
    raw = m.web_app_data.data
    try:
        data = json.loads(raw)
    except Exception:
        data = {"raw": raw}

    comp    = (data.get("company") or "").strip()[:20000]
    task    = (data.get("task") or "").strip()[:20000]
    contact = (data.get("contact") or "").strip()[:500]

    # 1) короткий хедер — почти небьётся
    header_ok = await send_lead_header("WebApp", m)

    # 2) полная карточка (с безопасной обрезкой)
    txt = build_lead("WebApp", m, comp, task, contact)
    delivered = await notify_admin(txt)

    # 3) подтверждение пользователю — точная фраза
    if delivered:
        ack = "Ваша анкета отправлена, спасибо! ✅\n(доставлено в лид-чат и админу)"
    else:
        ack = "Ваша анкета отправлена, спасибо! ✅\n" + (
            "(заголовок уже в лид-чате; полная карточка у админа)" if header_ok
            else "⚠️ Лид-чат временно недоступен — админ уведомлён."
        )
    await m.answer(ack, reply_markup=main_kb(is_private=(m.chat.type == "private"), is_admin=is_admin(m.from_user.id)))

# ---------- FASTAPI ----------
app = FastAPI(title="Vimly — Client Demo Bot (WebApp)")

# HTTP-fallback для браузера
@app.post("/webapp/submit")
async def webapp_submit(payload: dict = Body(...)):
    comp    = (payload.get("company") or "").strip()[:20000]
    task    = (payload.get("task") or "").strip()[:20000]
    contact = (payload.get("contact") or "").strip()[:500]
    await _send_to_leads("📥 Новая заявка (WebApp/браузер)")
    txt = (
        "🧪 Заявка (WebApp/браузер)\n"
        "От: неизвестно (браузер)\n"
        f"Компания: {esc(comp) or '—'}\n"
        f"Задача: {esc(task) or '—'}\n"
        f"Контакт: {esc(contact) or '—'}\n"
        f"UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
    )
    await notify_admin(txt)
    return {"ok": True}

# статика WebApp (html=True раздаёт index.html в папках)
STATIC_ROOT = os.path.join(os.path.dirname(__file__), "webapp")
if os.path.isdir(STATIC_ROOT):
    app.mount("/webapp", StaticFiles(directory=STATIC_ROOT, html=True), name="webapp")

# явный роут /webapp/quiz (+fallback HTML c Telegram WebApp JS)
FALLBACK_QUIZ_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Квиз-заявка</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,"Helvetica Neue",Arial;margin:0;padding:20px}
.card{max-width:640px;margin:0 auto;padding:20px;border:1px solid #eee;border-radius:16px}
label{display:block;margin:12px 0 6px;font-weight:600}
input,textarea{width:100%;padding:10px;border:1px solid #ccc;border-radius:10px}
button{margin-top:16px;padding:12px 16px;border:0;border-radius:12px;cursor:pointer}
button#send{background:#111;color:#fff}</style></head><body>
<div class="card"><h3>Квиз-заявка</h3>
<label>Описание компании</label><textarea id="company" rows="3" placeholder="Чем занимаетесь?"></textarea>
<label>Задача</label><textarea id="task" rows="3" placeholder="Что нужно сделать боту?"></textarea>
<label>Контакт в Telegram</label><input id="contact" placeholder="@username или телефон">
<button id="send">Отправить</button></div>
<script>(function(){
  const tg = window.Telegram && Telegram.WebApp ? Telegram.WebApp : null;
  const card = document.querySelector('.card');
  const btn = document.getElementById('send');
  async function send(){
    const payload = {
      company: document.getElementById('company').value||"",
      task: document.getElementById('task').value||"",
      contact: document.getElementById('contact').value||""
    };
    if (tg && tg.sendData){
      tg.sendData(JSON.stringify(payload));
      tg.close();
    } else {
      try{
        await fetch('/webapp/submit', {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify(payload)
        });
        card.innerHTML = '<h3>Ваша анкета отправлена, спасибо! ✅</h3><p>Мы свяжемся с вами в ближайшее время.</p>';
      }catch(e){
        card.insertAdjacentHTML('beforeend','<p style="color:#b00">Не удалось отправить заявку: '+e+'</p>');
      }
    }
  }
  if(tg){ tg.expand(); tg.ready(); }
  btn.addEventListener('click', send);
})();</script>
</body></html>"""

@app.get("/webapp/quiz", response_class=HTMLResponse)
@app.get("/webapp/quiz/", response_class=HTMLResponse)
async def webapp_quiz():
    index_path = os.path.join(STATIC_ROOT, "quiz", "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, media_type="text/html")
    return HTMLResponse(FALLBACK_QUIZ_HTML)

# фавикон
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    hero = os.path.join(os.path.dirname(__file__), "assets", "hero.png")
    if os.path.exists(hero):
        return FileResponse(hero, media_type="image/png")
    return Response(status_code=204)

# HEAD-хендлеры
@app.head("/")
async def head_root(): return Response(status_code=200)
@app.head("/healthz")
async def head_healthz(): return Response(status_code=200)

@app.get("/", response_class=HTMLResponse)
async def index(): return f"<h3>{esc(BRAND_NAME)} — {esc(BRAND_TAGLINE)}</h3>"
@app.get("/healthz", response_class=PlainTextResponse)
async def healthz(): return "ok"

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

# --- Error handler ---
@dp.error()
async def on_error(event):
    exc = getattr(event, "exception", None)
    try:
        await notify_admin(f"⚠️ Ошибка: {html.escape(repr(exc))}")
    except Exception:
        pass
    logging.exception("Handler error: %s", exc)

# ---------- LIFECYCLE ----------
@app.on_event("startup")
async def on_startup():
    global BOT_USERNAME
    try:
        me = await bot.get_me()
        BOT_USERNAME = me.username or BOT_USERNAME
    except Exception as e:
        log.warning("get_me failed: %s", e)

    if MODE == "webhook":
        if BASE_URL:
            url = f"{BASE_URL}{WEBHOOK_PATH}"
            log.info("Setting webhook to: %r", url)
            try:
                await bot.set_webhook(url=url, secret_token=WEBHOOK_SECRET or None, drop_pending_updates=True)
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

# ---------- LOCAL POLLING ----------
if __name__ == "__main__":
    async def _run():
        log.info("Starting polling...")
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    asyncio.run(_run())
