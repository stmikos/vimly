# -*- coding: utf-8 -*-
"""
Vimly — Client Demo Bot (FastAPI + aiogram 3.7+)
Строгая валидация квиза + надёжная доставка в лид-чат.
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

LEADS_RAW = (os.getenv("LEADS_CHAT_ID") or "").strip()         # "-100…", "-49…"(группа) или "@channel"
LEADS_THREAD_ID = int((os.getenv("LEADS_THREAD_ID") or "0").strip() or "0")
LEADS_FAIL_MSG = "⚠️ Лид-чат временно недоступен — админ уведомлён."

BASE_URL = _norm_base_url(os.getenv("BASE_URL"))
WEBHOOK_PATH = _norm_path(os.getenv("WEBHOOK_PATH") or "/telegram/webhook/vimly")
WEBHOOK_SECRET = (os.getenv("WEBHOOK_SECRET") or "").strip()
MODE = (os.getenv("MODE") or "webhook").strip().lower()        # webhook | polling

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
Store.last_admin_dm = {}  # {user_id: datetime_utc}
BOT_USERNAME = ""         # set on startup

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
                await bot.send_message(ADMIN_CHAT_ID,
                    "⚠️ Бот не может писать в лид-чат (возможно, кикнут/нет прав). "
                    "Проверьте, что бот в чате и LEADS_CHAT_ID корректен.")
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
    ts = Store.last_admin_dm.get(user_id)
    if not ts: return 0
    delta = (datetime.now(timezone.utc) - ts).total_seconds()
    left = int(ADMIN_DM_COOLDOWN_SEC - delta)
    return max(0, left)

def admin_dm_mark(user_id: int):
    Store.last_admin_dm[user_id] = datetime.now(timezone.utc)

def deep_link(suffix: str) -> str:
    su = (suffix or "").strip().replace(" ", "
