# -*- coding: utf-8 -*-
"""
Vimly — Client Demo Bot (FastAPI + aiogram 3.7+)
Пересборка: WebApp и браузерный квиз шлют ТОЛЬКО в лид-группу (одно сообщение).
Добавлены: /stats, явные логи WEBAPP DATA RAW, безопасные ответы, самотесты.
"""

import os, logging, re, asyncio, json, html, secrets
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Response, Body
from fastapi.responses import HTMLResponse, PlainTextResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, Update,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    ForceReply, FSInputFile,
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
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

LEADS_RAW = (os.getenv("LEADS_CHAT_ID") or "").strip()   # "-100…"(группа) или "@channel"
if not LEADS_RAW:
    logging.critical("Missing LEADS_CHAT_ID. Provide negative group ID like '-1001234567890' or '@channel'.")
    raise RuntimeError("Missing LEADS_CHAT_ID env var")
if not LEADS_RAW.startswith("@"):
    try:
        if int(LEADS_RAW) >= 0:
            raise ValueError
    except ValueError:
        logging.critical("Invalid LEADS_CHAT_ID %r. Use negative group ID like '-1001234567890' or '@channel'.", LEADS_RAW)
        raise RuntimeError("Invalid LEADS_CHAT_ID env var")

LEADS_THREAD_ID = int((os.getenv("LEADS_THREAD_ID") or "0").strip() or "0")  # 0 если тем нет
LEADS_FAIL_MSG = "⚠️ Лид-чат временно недоступен — админ уведомлён."

BASE_URL = _norm_base_url(os.getenv("BASE_URL"))
WEBHOOK_PATH = _norm_path(os.getenv("WEBHOOK_PATH") or "/telegram/webhook/vimly")
WEBHOOK_SECRET = (os.getenv("WEBHOOK_SECRET") or "").strip()
MODE = (os.getenv("MODE") or "webhook").strip().lower()  # webhook | polling
ADMIN_DM_COOLDOWN_SEC = int((os.getenv("ADMIN_DM_COOLDOWN_SEC") or "60").strip() or "60")

# ---------- BRAND ----------
BRAND_NAME = (os.getenv("BRAND_NAME") or "Vimly").strip()
BRAND_TAGLINE = (os.getenv("BRAND_TAGLINE") or "Боты, которые продают").strip()
BRAND_TG = (os.getenv("BRAND_TG") or "@Vimly_bot").strip()
BRAND_SITE = (os.getenv("BRAND_SITE") or "").strip()

# ---------- LOG ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("vimly-webapp")
log.info("Leads target (raw): %r  thread: %s", LEADS_RAW, LEADS_THREAD_ID or "—")

# ---------- AIOGRAM ----------
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ---------- STORE ----------
class Store:
    accepting = True
    started_at = datetime.now(timezone.utc)
    users = set()
    stats = {"starts": 0, "quiz": 0, "orders": 0, "webquiz": 0, "contact_msgs": 0}
Store.promos = {}
Store.gift_claimed = set()
Store.last_admin_dm = {}
BOT_USERNAME = ""

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
    if BRAND_SITE: parts.append(esc(BRAND_SITE))
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
    target = parse_leads_target(LEADS_RAW)
    if not target:
        log.error("LEADS_CHAT_ID invalid/empty: %r", LEADS_RAW)
        return False
    try:
        # Проверим необходимость thread_id (если включены темы)
        try:
            chat = await bot.get_chat(target)
            if getattr(chat, "is_forum", False) and not LEADS_THREAD_ID:
                log.error("LEADS_THREAD_ID required: chat has topics enabled")
                if ADMIN_CHAT_ID:
                    try:
                        await bot.send_message(
                            ADMIN_CHAT_ID,
                            "⚠️ В лид-чате включены темы — укажите корректный LEADS_THREAD_ID.",
                            disable_notification=True,
                        )
                    except Exception:
                        pass
                return False
        except Exception as e:
            log.debug("get_chat failed for %r: %s", LEADS_RAW, e)

        kwargs = {"disable_web_page_preview": True}
        if LEADS_THREAD_ID:
            kwargs["message_thread_id"] = LEADS_THREAD_ID
        msg = await bot.send_message(target, text, **kwargs)
        try:
            chat = await bot.get_chat(target)
            log.info("LEADS OK → %s (%s), msg_id=%s", getattr(chat, "title", "—"), chat.id, msg.message_id)
        except Exception:
            log.info("LEADS OK → chat=%r, msg_id=%s", LEADS_RAW, getattr(msg, "message_id", "—"))
        return True
    except TelegramForbiddenError as e:
        log.error("LEADS forbidden: %s (бот кикнут/нет прав)", e)
        if ADMIN_CHAT_ID:
            try:
                await bot.send_message(
                    ADMIN_CHAT_ID,
                    "⚠️ Бот не может писать в лид-чат (возможно, кикнут/нет прав). Проверьте, что бот в чате и LEADS_CHAT_ID корректен."
                )
            except Exception: pass
        return False
    except Exception as e:
        log.warning("LEADS FAIL → %r | %s", LEADS_RAW, e)
        if ADMIN_CHAT_ID:
            try:
                await bot.send_message(
                    ADMIN_CHAT_ID,
                    f"⚠️ LEADS FAIL → <code>{esc(str(e))}</code>\n(target={esc(str(LEADS_RAW))}, thread={LEADS_THREAD_ID or '—'})",
                    disable_notification=True
                )
            except Exception: pass
        return False

async def notify_admin(text: str) -> bool:
    """Шлёт ТОЛЬКО админу в ЛС. Не дублирует в лид-чат."""
    ok = True
    if ADMIN_CHAT_ID:
        try:
            await bot.send_message(ADMIN_CHAT_ID, text, disable_notification=True, disable_web_page_preview=True)
        except Exception as e:
            log.warning("notify_admin failed: %s", e)
            ok = False
    return ok

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
    ts = Store.last_admin_dm.get(user_id)
    if not ts: return 0
    delta = (datetime.now(timezone.utc) - ts).total_seconds()
    left = int(ADMIN_DM_COOLDOWN_SEC - delta)
    return max(0, left)

def admin_dm_mark(user_id: int):
    Store.last_admin_dm[user_id] = datetime.now(timezone.utc)

def deep_link(suffix: str) -> str:
    su = (suffix or "").strip().replace(" ", "_")
    return f"https://t.me/{BOT_USERNAME}?start={su}" if BOT_USERNAME else ""

def force_reply_if_needed(chat_type: str, placeholder: str) -> Optional[ForceReply]:
    return ForceReply(selective=True, input_field_placeholder=placeholder) if chat_type != "private" else None

# --- строгая валидация квиза ---
USERNAME_RE = re.compile(r"^@[a-zA-Z0-9_]{5,}$")
EMAIL_RE    = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def valid_contact(s: str) -> bool:
    s = (s or "").strip()
    if USERNAME_RE.match(s): return True
    if EMAIL_RE.match(s): return True
    d = re.sub(r"\D+", "", s)
    return 7 <= len(d) <= 15

def validate_web_quiz(company: str, task: str, contact: str) -> tuple[bool, str]:
    company = (company or "").strip()
    task    = (task or "").strip()
    contact = (contact or "").strip()
    if not company or not task or not contact:
        return False, "Заполните все поля: описание, задача и контакт."
    if len(company) < 3:
        return False, "Описание компании слишком короткое (мин. 3 символа)."
    if len(task) < 5:
        return False, "Задача слишком короткая (мин. 5 символов)."
    if not valid_contact(contact):
        return False, "Контакт укажи как @username, телефон или email."
    return True, ""

MAX_TG = 3900
def build_lead(kind: str, m: Optional[Message], company: str, task: str, contact: str) -> str:
    who = f"От: {ufmt(m)}\n" if m else "От: неизвестно (браузер)\n"
    comp = (company or "").strip()
    tsk  = (task or "").strip()
    cnt  = (contact or "").strip()
    base = f"🧪 Заявка ({kind})\n{who}"
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
        return s[: n-1] + "…" if len(s) > n else s
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

# ---------- UI ----------
def main_kb(is_private: bool, is_admin: bool) -> InlineKeyboardMarkup:
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
@dp.message()
async def any_msg_log(m: Message):
    logging.info("ANY MSG: chat=%s type=%s text=%r", m.chat.id, m.chat.type, (m.text or ""))

@dp.message(CommandStart())
async def on_start(m: Message, state: FSMContext):
    Store.stats["starts"] += 1
    Store.users.add(m.from_user.id)
    parts = (m.text or "").split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""

    hero = os.path.join(os.path.dirname(__file__), "assets", "hero.png")
    try:
        await m.answer_photo(FSInputFile(hero), caption=header())
    except Exception:
        await m.answer(header())

    if arg == "contact":
        await state.set_state(AdminMsg.text)
        kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True,
                                 keyboard=[[KeyboardButton(text="Отмена")]])
        note = f"(антиспам: не чаще раз в {ADMIN_DM_COOLDOWN_SEC} сек)"
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
    await m.answer("Главное меню:", reply_markup=main_kb(is_private=(m.chat.type == "private"),
                                                         is_admin=is_admin(m.from_user.id)))

@dp.message(Command("stats"))
async def on_stats(m: Message):
    s = Store.stats
    await m.answer(f"stats → starts={s['starts']}, webquiz={s['webquiz']}, chatquiz={s['quiz']}, orders={s['orders']}")

@dp.message(Command("chatid"))
async def cmd_chatid(m: Message): await m.answer(f"chat_id: <code>{m.chat.id}</code>")

@dp.message(Command("threadid"))
async def cmd_threadid(m: Message): await m.answer(f"thread_id: <code>{getattr(m, 'message_thread_id', None)}</code>")

# --- Диагностика/управление лид-чатом ---
@dp.message(Command("get_leads"))
async def cmd_get_leads(m: Message):
    if m.from_user.id != ADMIN_CHAT_ID: return
    await m.answer(f"LEADS_CHAT_ID: <code>{esc(LEADS_RAW)}</code>\nLEADS_THREAD_ID: <code>{LEADS_THREAD_ID or '—'}</code>")

@dp.message(Command("set_leads"))
async def cmd_set_leads(m: Message):
    if m.from_user.id != ADMIN_CHAT_ID: return
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await m.answer("Использование: /set_leads -1001234567890 ИЛИ /set_leads @channel")
    global LEADS_RAW
    LEADS_RAW = parts[1].strip()
    await m.answer(f"LEADS_CHAT_ID → <code>{esc(LEADS_RAW)}</code>")

@dp.message(Command("leads_probe"))
async def leads_probe(m: Message):
    if m.from_user.id != ADMIN_CHAT_ID: return
    ok = await _send_to_leads("🔔 PROBE to leads")
    await m.answer(f"leads_probe → {'OK' if ok else 'FAIL'} (target={esc(str(LEADS_RAW))}, thread={LEADS_THREAD_ID or '—'})")

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
            f"• can_post_messages: {g(member, 'can_post_messages', '—')}\n"
        )
        await m.answer(info)
    except Exception as e:
        await m.answer(f"❌ Не удалось прочитать чат {target_raw!r}:\n<code>{e}</code>")

@dp.message(Command("test_leads"))
async def test_leads_cmd(m: Message):
    if m.from_user.id != ADMIN_CHAT_ID: return
    try:
        kwargs = {}
        if LEADS_THREAD_ID:
            kwargs["message_thread_id"] = LEADS_THREAD_ID
        await bot.send_message(parse_leads_target(LEADS_RAW), "🔔 Тест в чат лидов: работает ✅", **kwargs)
        await m.answer(f"OK → {LEADS_RAW!r} (thread={LEADS_THREAD_ID or '—'})")
    except Exception as e:
        await m.answer(f"❌ Не отправилось в {LEADS_RAW!r}:\n<code>{e}</code>")

# --- Меню / контент ---
@dp.callback_query(F.data == "hide_menu")
async def cb_hide_menu(c: CallbackQuery):
    try: await c.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest: pass
    try: await c.message.edit_text("Меню скрыто. Напишите /menu чтобы открыть.")
    except TelegramBadRequest: await c.message.answer("Меню скрыто. Напишите /menu чтобы открыть.")
    await c.answer()

@dp.callback_query(F.data == "go_menu")
async def cb_menu(c: CallbackQuery): await safe_edit(c, "Главное меню:")

@dp.callback_query(F.data == "go_process")
async def cb_process(c: CallbackQuery):
    txt = ("Как запускаем за 1–3 дня:\n"
           "1) <b>Созвон 15 минут</b> — фиксируем цели\n"
           "2) <b>MVP</b> — меню + квиз + админ-чат\n"
           "3) <b>Запуск</b> — подключаем Sheets/оплату/канал\n"
           "4) <b>Поддержка</b> — рассылки, правки, отчёты")
    await safe_edit(c, txt); await c.answer()

@dp.callback_query(F.data == "go_cases")
async def cb_cases(c: CallbackQuery):
    txt = ("Кейсы (демо):\n"
           "• Барбершоп — запись и отзывы\n"
           "• Пекарня — квиз + купоны\n"
           "• Автор-канал — оплата → доступ\n"
           "• Коворкинг — афиша/RSVP")
    await safe_edit(c, txt); await c.answer()

@dp.callback_query(F.data == "go_prices")
async def cb_prices(c: CallbackQuery):
    txt = ("<b>Пакеты и цены:</b>\n\n"
           "• <b>Lite</b> — 15–20k ₽\n"
           "• <b>Standard</b> — 25–45k ₽\n"
           "• <b>Pro</b> — 50–90k ₽\n\n"
           "<i>Поддержка 3–10k ₽/мес</i>")
    await safe_edit(c, txt); await c.answer()

# --- Контакты + «написать админу» ---
@dp.callback_query(F.data == "go_contacts")
async def cb_contacts(c: CallbackQuery, state: FSMContext):
    if c.message.chat.type != "private":
        url = deep_link("contact")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Открыть диалог с админом", url=url)],
            [InlineKeyboardButton(text="⬅️ Меню", callback_data="go_menu")],
        ])
        await safe_edit(c, "<b>Контакты</b>\nНажмите кнопку, чтобы написать админу в ЛС.", kb); await c.answer(); return

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Меню", callback_data="go_menu")]])
    note = f"(антиспам: не чаще {ADMIN_DM_COOLDOWN_SEC} сек)"
    await safe_edit(c, f"<b>Напишите сообщение админу.</b>\n{note}", kb)
    await c.message.answer("Пришлите текст/медиа. «Отмена» — выйти.",
                           reply_markup=ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True,
                                                            keyboard=[[KeyboardButton(text="Отмена")]]))
    await state.set_state(AdminMsg.text); await c.answer()

@dp.callback_query(F.data == "admin_open")
async def cb_admin_open(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Только для владельца бота", show_alert=True); return
    uptime = datetime.now(timezone.utc) - Store.started_at
    s = Store.stats
    txt = (f"<b>🛠 Админ-панель</b>\n"
           f"Uptime: {str(uptime).split('.',1)[0]}\n"
           f"Уникальных пользователей: <b>{len(Store.users)}</b>\n"
           f"Starts: {s['starts']} | WebQuiz: {s['webquiz']} | ChatQuiz: {s['quiz']} | Orders: {s['orders']} | Msgs→Admin: {s['contact_msgs']}\n")
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
    await m.answer("Главное меню:",
                   reply_markup=main_kb(is_private=(m.chat.type == "private"), is_admin=is_admin(m.from_user.id)))

@dp.message(AdminMsg.text, F.text)
async def contact_text(m: Message, state: FSMContext):
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
    await m.answer("Главное меню:",
                   reply_markup=main_kb(is_private=(m.chat.type == "private"), is_admin=is_admin(m.from_user.id)))

@dp.message(AdminMsg.text)
async def contact_any(m: Message, state: FSMContext):
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
    await m.answer("Главное меню:",
                   reply_markup=main_kb(is_private=(m.chat.type == "private"), is_admin=is_admin(m.from_user.id)))

# --- Подарок ---
@dp.callback_query(F.data == "go_gift")
async def cb_gift(c: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Чек-лист PDF", callback_data="gift_pdf"),
         InlineKeyboardButton(text="🎟 Промокод −20% (72ч)", callback_data="gift_promo")],
        [InlineKeyboardButton(text="⬅️ Меню", callback_data="go_menu")]
    ])
    await safe_edit(c, "<b>Выберите подарок</b>: чек-лист PDF или промокод −20% на Lite (72ч).", kb); await c.answer()

@dp.callback_query(F.data == "gift_pdf")
async def cb_gift_pdf(c: CallbackQuery):
    uid = c.from_user.id
    pdf_path = os.path.join(os.path.dirname(__file__), "assets", "gifts", "checklist.pdf")
    caption = ("<b>Чек-лист: «Бот, который окупится за 48 часов»</b>\n"
               "Цель • Меню • УТП • Квиз • Лиды • Автоответ • Оффер • Кейсы • Память • Правки • Рассылки • Цифры")
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
    txt = ("<b>Ваш промокод: </b><code>{code}</code>\n"
           "Скидка: −20% на Lite, до {exp}\n"
           "Примените при подтверждении заказа.").format(code=esc(promo["code"]), exp=esc(promo["expires_utc"]))
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
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, keyboard=[[KeyboardButton(text="Отправить мой номер", request_contact=True)]])
    await c.message.answer("Оставьте телефон или напишите контакт (телеграм/почта):", reply_markup=kb)
    await c.answer()

@dp.message(Order.contact, F.contact)
async def order_contact_obj(m: Message, state: FSMContext):
    phone = sanitize_phone(m.contact.phone_number)
    if not phone:
        return await m.answer("Телефон некорректный. Пришлите ещё раз или укажите @username/email.")
    await finalize_order(m, state, phone=phone)

@dp.message(Order.contact)
async def order_contact_text(m: Message, state: FSMContext):
    txt = (m.text or "").strip()
    phone = sanitize_phone(txt)
    if not (phone or valid_contact(txt)):
        await m.answer("Контакт обязателен: @username, телефон (7–15 цифр) или email.")
        return
    await finalize_order(m, state, phone=phone, raw=txt)

async def finalize_order(m: Message, state: FSMContext, phone: Optional[str], raw: Optional[str] = None):
    await state.clear()
    Store.stats["orders"] += 1
    clean = phone or (raw.strip() if raw else "—")
    msg = ("🛒 Заказ/контакт\n"
           f"От: {ufmt(m)}\n"
           f"Контакт: {esc(clean)}\n"
           f"UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}")
    await m.answer("Спасибо! Мы на связи.", reply_markup=ReplyKeyboardRemove())
    await m.answer("Главное меню:", reply_markup=main_kb(is_private=(m.chat.type == "private"), is_admin=is_admin(m.from_user.id)))
    delivered = await _send_to_leads(msg)  # только в группу
    if not delivered and ADMIN_CHAT_ID:
        await notify_admin("⚠️ Лид-чат недоступен, проверьте окружение/права.")

# --- Чат-квиз (ForceReply) ---
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
    txt = (m.text or "").strip()
    if not txt:
        return await m.answer("Поле обязательно. Укажи нишу и город (не пусто).")
    await state.update_data(niche=txt[:200])
    await state.set_state(Quiz.goal)
    kb = force_reply_if_needed(m.chat.type, "Цель бота")
    if kb:
        await m.answer("Цель бота? (2/3) — заявки, запись, оплата, отзывы…\nОтветьте на это сообщение:", reply_markup=kb)
    else:
        await m.answer("Цель бота? (2/3) — заявки, запись, оплата, отзывы…")

@dp.message(Quiz.goal)
async def quiz_goal(m: Message, state: FSMContext):
    txt = (m.text or "").strip()
    if not txt:
        return await m.answer("Поле обязательно. Опиши цель бота (не пусто).")
    await state.update_data(goal=txt[:300])
    await state.set_state(Quiz.deadline)
    kb = force_reply_if_needed(m.chat.type, "Срок запуска")
    if kb:
        await m.answer("Срок запуска? (3/3) — например: 2–3 дня / дата\nОтветьте на это сообщение:", reply_markup=kb)
    else:
        await m.answer("Срок запуска? (3/3) — например: 2–3 дня / дата")

@dp.message(Quiz.deadline)
async def quiz_done(m: Message, state: FSMContext):
    txt = (m.text or "").strip()
    if not txt:
        return await m.answer("Поле обязательно. Укажи срок запуска (не пусто).")
    data = await state.update_data(deadline=txt[:100])
    await state.clear()
    Store.stats["quiz"] += 1
    msg = ("🆕 Заявка (квиз-чат)\n"
           f"От: {ufmt(m)}\n"
           f"Ниша: {esc(data.get('niche'))}\n"
           f"Цель: {esc(data.get('goal'))}\n"
           f"Срок: {esc(data.get('deadline'))}\n"
           f"UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}")
    delivered = await _send_to_leads(msg)  # только в группу
    ack = "Ваша анкета отправлена, спасибо! ✅"
    if not delivered and ADMIN_CHAT_ID:
        await notify_admin("⚠️ Лид-чат недоступен, проверьте окружение/права.")
        ack += f"\n{LEADS_FAIL_MSG}"
    await m.answer(ack, reply_markup=main_kb(is_private=(m.chat.type == "private"),
                                             is_admin=is_admin(m.from_user.id)))

# --- Приём данных из Telegram WebApp (строгая валидация) ---
@dp.message(F.web_app_data)
async def on_webapp_data(m: Message):
    Store.stats["webquiz"] += 1

    raw = m.web_app_data.data
    log.info("WEBAPP DATA RAW: %s", raw)

    try:
        data = json.loads(raw)
    except Exception:
        await m.answer("❗️Неверный формат данных. Попробуйте ещё раз.")
        return

    comp    = (data.get("company") or "").strip()[:20000]
    task    = (data.get("task") or "").strip()[:20000]
    contact = (data.get("contact") or "").strip()[:500]

    ok, err = validate_web_quiz(comp, task, contact)
    if not ok:
        await m.answer(f"❗️{err}")
        return

    await m.answer("📥 Приняли данные из WebApp, отправляю в лид-чат…")

    txt = build_lead("WebApp", m, comp, task, contact)
    delivered = await _send_to_leads(txt)

    ack = "Ваша анкета отправлена, спасибо! ✅"
    if not delivered:
        ack += "\n" + LEADS_FAIL_MSG
        if ADMIN_CHAT_ID:
            await notify_admin("⚠️ Лид-чат недоступен, проверьте окружение/права.")
    await m.answer(ack, reply_markup=main_kb(is_private=(m.chat.type == "private"),
                                             is_admin=is_admin(m.from_user.id)))

# ---------- FASTAPI ----------
app = FastAPI(title="Vimly — Client Demo Bot (WebApp)")

# HTTP-fallback для браузера (строгая валидация) — ТОЛЬКО в группу
@app.post("/webapp/submit")
async def webapp_submit(payload: dict = Body(...)):
    comp    = (payload.get("company") or "").strip()[:20000]
    task    = (payload.get("task") or "").strip()[:20000]
    contact = (payload.get("contact") or "").strip()[:500]

    ok, err = validate_web_quiz(comp, task, contact)
    if not ok:
        return JSONResponse({"ok": False, "error": err}, status_code=400)

    txt = build_lead("WebApp/браузер", None, comp, task, contact)
    delivered = await _send_to_leads(txt)
    if not delivered:
        if ADMIN_CHAT_ID:
            await notify_admin("⚠️ Лид-чат недоступен, проверьте окружение/права.")
        return JSONResponse({"ok": False, "error": "leads_unavailable"}, status_code=503)
    return {"ok": True}

# статика WebApp (если есть папка webapp)
STATIC_ROOT = os.path.join(os.path.dirname(__file__), "webapp")
if os.path.isdir(STATIC_ROOT):
    app.mount("/webapp", StaticFiles(directory=STATIC_ROOT, html=True), name="webapp")

# fallback HTML /webapp/quiz (клиентская валидация и запрет кнопки при пустых полях)
FALLBACK_QUIZ_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Квиз-заявка</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,"Helvetica Neue",Arial;margin:0;padding:20px;background:#fafafa}
.card{max-width:640px;margin:0 auto;padding:20px;border:1px solid #eee;border-radius:16px;background:#fff;box-shadow:0 1px 8px rgba(0,0,0,.04)}
label{display:block;margin:12px 0 6px;font-weight:600}
input,textarea{width:100%;padding:10px;border:1px solid #ccc;border-radius:10px}
small.err{display:block;color:#b00020;margin-top:6px}
button{margin-top:16px;padding:12px 16px;border:0;border-radius:12px;cursor:pointer}
button#send{background:#111;color:#fff;opacity:.9}
button#send[disabled]{opacity:.4;cursor:not-allowed}
.notice{margin-top:8px;color:#666}
.warn{display:none;padding:12px;border-radius:10px;margin:10px 0;background:#fff3cd;border:1px solid #ffeeba;color:#856404}
</style></head><body>
<div class="card">
  <h3>Квиз-заявка</h3>

  <div id="warn" class="warn">
    Похоже, вы открыли форму в браузере. Чтобы анкета ушла прямо в Telegram и бот ответил в чате,
    откройте её из диалога с ботом по кнопке «🧪 Квиз (в Telegram)».
  </div>

  <label>Описание компании</label>
  <textarea id="company" rows="3" placeholder="Чем занимаетесь?" required minlength="3"></textarea>
  <small id="e_company" class="err" style="display:none"></small>

  <label>Задача</label>
  <textarea id="task" rows="3" placeholder="Что нужно сделать боту?" required minlength="5"></textarea>
  <small id="e_task" class="err" style="display:none"></small>

  <label>Контакт</label>
  <input id="contact" placeholder="@username или телефон/email" required>
  <small id="e_contact" class="err" style="display:none"></small>

  <button id="send" disabled type="button">Отправить</button>
  <div class="notice">Все поля обязательны</div>
</div>
<script>(function(){
  const tg = (window.Telegram && Telegram.WebApp) ? Telegram.WebApp : null;
  const $  = (id)=>document.getElementById(id);
  const fields = ["company","task","contact"];
  const errs = {company:$("e_company"), task:$("e_task"), contact:$("e_contact")};
  const btn = $("send");

  // показать предупреждение, если это не Telegram WebView
  if (!tg) { $("warn").style.display = "block"; }

  function isValidContact(v){
    v = (v||"").trim();
    if (/^@[a-zA-Z0-9_]{5,}$/.test(v)) return true;
    if (/^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$/.test(v)) return true;
    const d = v.replace(/\\D+/g,"");
    return d.length>=7 && d.length<=15;
  }

  function validate(show){
    let ok = true;
    const company = $("company").value.trim();
    const task    = $("task").value.trim();
    const contact = $("contact").value.trim();

    if (!company || company.length<3){ ok=false; if(show){ errs.company.textContent="Минимум 3 символа"; errs.company.style.display="block"; } }
    else if(show){ errs.company.style.display="none"; }

    if (!task || task.length<5){ ok=false; if(show){ errs.task.textContent="Минимум 5 символов"; errs.task.style.display="block"; } }
    else if(show){ errs.task.style.display="none"; }

    if (!contact || !isValidContact(contact)){ ok=false; if(show){ errs.contact.textContent="Укажи @username, телефон или email"; errs.contact.style.display="block"; } }
    else if(show){ errs.contact.style.display="none"; }

    btn.disabled = !ok;
    if (tg && tg.MainButton){
      tg.MainButton.setText("Отправить");
      if(ok){ tg.MainButton.show(); tg.MainButton.enable(); } else { tg.MainButton.disable(); }
    }
    return ok;
  }

  fields.forEach(id=>$(id).addEventListener("input", ()=>validate(false)));

  async function send(){
    const ok = validate(true);
    if(!ok) return;

    const payload = {
      company: $("company").value.trim(),
      task: $("task").value.trim(),
      contact: $("contact").value.trim(),
      nonce: Math.random().toString(36).slice(2) + Date.now()  // для антидублей на сервере
    };

    // 1) Пытаемся отдать данные в Telegram (если это WebApp)
    try{
      if (tg && tg.sendData) {
        tg.sendData(JSON.stringify(payload));
      }
    }catch(e){ console.log("tg.sendData failed:", e); }

    // 2) Всегда бэкапим на сервер (уйдёт в лид-группу через /webapp/submit)
    try{
      const r = await fetch('/webapp/submit', {
        method:'POST',
        headers:{'Content-Type':'application/json','X-From-WebApp':'1'},
        body: JSON.stringify(payload)
      });
      if(!r.ok){
        const j = await r.json().catch(()=>({error:"Ошибка отправки"}));
        throw new Error(j.error||("HTTP "+r.status));
      }
      document.querySelector('.card').innerHTML =
        '<h3>Ваша анкета отправлена, спасибо! ✅</h3><p>Мы свяжемся с вами в ближайшее время.</p>';
    }catch(e){
      alert(e.message||e);
    }finally{
      if (tg && tg.close) tg.close();
    }
  }

  if(tg){ tg.expand(); tg.ready(); }
  btn.addEventListener('click', send);
  if (tg && tg.MainButton){ tg.MainButton.onClick(send); }
  validate(false);
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
    # временный лог
    log.info("Webhook headers: %r", dict(request.headers))
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
async def on_error(event, exception):
    try:
        if ADMIN_CHAT_ID:
            await bot.send_message(ADMIN_CHAT_ID, f"⚠️ Ошибка: <code>{esc(repr(exception))}</code>")
    except Exception:
        pass
    logging.exception("Handler error: %s", exception)

# ---------- LIFECYCLE ----------
@app.on_event("startup")
async def on_startup():
    global BOT_USERNAME
    me = None
    try:
        me = await bot.get_me()
        BOT_USERNAME = me.username or BOT_USERNAME
    except Exception as e:
        log.warning("get_me failed: %s", e)

    # Проверяем, что бот может писать в лид-чат; иначе отключаем приём заявок
    target = parse_leads_target(LEADS_RAW)
    if not target:
        log.critical("LEADS_CHAT_ID invalid/empty: %r", LEADS_RAW)
        Store.accepting = False
        if ADMIN_CHAT_ID:
            try:
                await bot.send_message(ADMIN_CHAT_ID, "⚠️ LEADS_CHAT_ID invalid/empty. Приём заявок отключён.")
            except Exception:
                pass
    else:
        try:
            if me is None:
                me = await bot.get_me()
            cm = await bot.get_chat_member(target, me.id)
            no_send = (getattr(cm, "status", None) in {"left", "kicked"})
            if hasattr(cm, "can_send_messages"):
                no_send = no_send or (not getattr(cm, "can_send_messages"))
            if no_send:
                raise TelegramForbiddenError("Bot has no send rights")
        except TelegramForbiddenError as e:
            log.critical("Bot lacks permissions for LEADS_CHAT_ID %r: %s", LEADS_RAW, e)
            Store.accepting = False
            if ADMIN_CHAT_ID:
                try:
                    await bot.send_message(ADMIN_CHAT_ID, "⚠️ Бот не может писать в лид-чат. Приём заявок отключён.")
                except Exception:
                    pass
        except Exception as e:
            log.critical("Failed to verify LEADS_CHAT_ID %r: %s", LEADS_RAW, e)
            Store.accepting = False
            if ADMIN_CHAT_ID:
                try:
                    await bot.send_message(ADMIN_CHAT_ID, f"⚠️ Не удалось проверить лид-чат: <code>{esc(str(e))}</code>")
                except Exception:
                    pass

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
