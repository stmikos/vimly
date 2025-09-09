# -*- coding: utf-8 -*-
"""
Vimly — Client Demo Bot (FastAPI + aiogram 3.7+)

Включено:
- HTML parse mode (устраняет markdown-ошибки)
- /start: hero-картинка отдельно, меню отдельным сообщением
- «🧪 Квиз-заявка» сразу открывает WebApp (fallback — чат-квиз)
- «↘ Скрыть меню» — сворачивает клавиатуру
- safe_edit: корректно редактирует caption/text
- Лиды в ADMIN + LEADS_CHAT (поддержка тем через LEADS_THREAD_ID) с диагностикой
- Диагностика: /check_leads, /test_leads, /chatid, /threadid
- 🎁 Подарок: PDF чек-лист + промокод −20% на 72 часа
- Статика /webapp/quiz/ (+favicon), резервная встроенная HTML-форма
- HEAD-роуты для пингов Render
"""

import os, logging, re, asyncio, json, html, secrets
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, FileResponse
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

# Лид-чат: можно -100… (группа/канал) или @channelusername (канал)
LEADS_RAW = (os.getenv("LEADS_CHAT_ID") or "").strip()
LEADS_THREAD_ID = int((os.getenv("LEADS_THREAD_ID") or "0").strip() or "0")

BASE_URL = _norm_base_url(os.getenv("BASE_URL"))
WEBHOOK_PATH = _norm_path(os.getenv("WEBHOOK_PATH") or "/telegram/webhook/vimly")
WEBHOOK_SECRET = (os.getenv("WEBHOOK_SECRET") or "").strip()
MODE = (os.getenv("MODE") or "webhook").strip().lower()  # webhook | polling

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
    stats = {"starts": 0, "quiz": 0, "orders": 0, "webquiz": 0}

# Подарки/промо (in-memory)
Store.promos = {}           # user_id -> {"code": str, "expires_utc": str}
Store.gift_claimed = set()  # user_id

# ---------- FSM ----------
class Quiz(StatesGroup):
    niche = State()
    goal = State()
    deadline = State()

class Order(StatesGroup):
    contact = State()

# ---------- HELPERS ----------
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

def parse_leads_target(s: str):
    s = (s or "").strip()
    if not s:
        return None
    if s.startswith("@"):
        return s  # username канала
    try:
        return int(s)  # числовой id (-100…)
    except ValueError:
        return None

async def _send_to_leads(text: str) -> bool:
    """Постит в лид-чат. Возвращает True при успехе, False при ошибке."""
    target = parse_leads_target(LEADS_RAW)
    if not target:
        return False
    try:
        kwargs = {}
        if LEADS_THREAD_ID:
            kwargs["message_thread_id"] = LEADS_THREAD_ID
        await bot.send_message(target, text, **kwargs)
        log.info("Lead routed to %r (thread=%s)", LEADS_RAW, LEADS_THREAD_ID or "—")
        return True
    except Exception as e:
        log.warning("notify_leads failed: %s", e)
        if ADMIN_CHAT_ID:
            try:
                await bot.send_message(
                    ADMIN_CHAT_ID,
                    f"⚠️ Не удалось отправить в лид-чат {LEADS_RAW!r}:\n<code>{esc(str(e))}</code>"
                )
            except Exception:
                pass
        return False

async def notify_admin(text: str) -> bool:
    # Личка владельца (всегда пробуем)
    if ADMIN_CHAT_ID:
        try:
            await bot.send_message(ADMIN_CHAT_ID, text, disable_notification=True)
        except Exception as e:
            log.warning("notify_admin failed: %s", e)
    # Возвращаем результат доставки в лид-чат
    return await _send_to_leads(text)

async def safe_edit(c: CallbackQuery, html_text: str, kb: Optional[InlineKeyboardMarkup] = None):
    """Редактируем caption/text по типу сообщения; если нельзя — отправляем новое."""
    kb = kb or main_kb()
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
    suffix = secrets.token_hex(2).upper()  # напр. A9F3
    code = f"VIM-{str(user_id)[-4:]}-{suffix}"
    expires = datetime.now(timezone.utc) + timedelta(hours=72)
    data = {"code": code, "expires_utc": expires.strftime("%Y-%m-%d %H:%M UTC")}
    Store.promos[user_id] = data
    return data

MAX_TG = 3900  # запас от лимита 4096
def build_lead(kind: str, m: Message, company: str, task: str, contact: str) -> str:
    """Собирает текст лида и подрезает company/task, если слишком длинные."""
    base_head = f"🧪 Заявка ({kind})\nОт: {ufmt(m)}\n"
    comp = (company or "").strip()
    tsk  = (task or "").strip()
    cnt  = (contact or "").strip()
    body = (
        f"Компания: {esc(comp) or '—'}\n"
        f"Задача: {esc(tsk) or '—'}\n"
        f"Контакт: {esc(cnt) or '—'}\n"
        f"UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
    )
    txt = base_head + body
    if len(txt) <= MAX_TG:
        return txt

    comp_max = max(150, int((MAX_TG - len(base_head) - 100) * 0.45))
    tsk_max  = max(150, int((MAX_TG - len(base_head) - 100) * 0.45))
    def cut(s: str, n: int) -> str:
        s = s.strip()
        return (s[: n-1] + "…") if len(s) > n else s
    comp_cut = cut(comp, comp_max)
    tsk_cut  = cut(tsk,  tsk_max)
    body2 = (
        f"Компания: {esc(comp_cut) or '—'}\n"
        f"Задача: {esc(tsk_cut) or '—'}\n"
        f"Контакт: {esc(cnt) or '—'}\n"
        f"(обрезано)\n"
        f"UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
    )
    txt2 = base_head + body2
    if len(txt2) > MAX_TG:
        tsk_cut = cut(tsk_cut, max(120, tsk_max - (len(txt2) - MAX_TG + 20)))
        body2 = (
            f"Компания: {esc(comp_cut) or '—'}\n"
            f"Задача: {esc(tsk_cut) or '—'}\n"
            f"Контакт: {esc(cnt) or '—'}\n"
            f"(обрезано)\n"
            f"UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
        )
        txt2 = base_head + body2
    return txt2

# ---------- UI ----------
def main_kb() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="🧭 Процесс", callback_data="go_process"),
            InlineKeyboardButton(text="💼 Кейсы (демо)", callback_data="go_cases"),
        ],
        [
            InlineKeyboardButton(
                text="🧪 Квиз-заявка",
                web_app=WebAppInfo(url=f"{BASE_URL}/webapp/quiz/")
            ) if BASE_URL else InlineKeyboardButton(
                text="🧪 Квиз-заявка (в чате)", callback_data="go_quiz"
            ),
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
        [InlineKeyboardButton(text="↘ Скрыть меню", callback_data="hide_menu")],
        [InlineKeyboardButton(text="🛠 Админ", callback_data="admin_open")],
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
            InlineKeyboardButton(text="📣 Тест-рассылка", callback_data="admin_broadcast"),
            InlineKeyboardButton(text="⬅️ Меню", callback_data="go_menu"),
        ]
    ])

# ---------- HANDLERS ----------
@dp.message(CommandStart())
async def on_start(m: Message):
    Store.stats["starts"] += 1
    hero = os.path.join(os.path.dirname(__file__), "assets", "hero.png")
    try:
        await m.answer_photo(FSInputFile(hero), caption=header())
    except Exception:
        pass
    await m.answer("Демо-бот: квиз, кейсы, запись. Нажмите кнопку ниже 👇", reply_markup=main_kb())

@dp.message(Command("menu"))
async def on_menu(m: Message):
    await m.answer("Главное меню:", reply_markup=main_kb())

@dp.message(Command("admin"))
async def on_admin(m: Message):
    if m.from_user.id != ADMIN_CHAT_ID:
        return await m.answer("Админ-панель доступна владельцу бота.")
    await m.answer("Админ-панель:", reply_markup=admin_kb())

@dp.callback_query(F.data == "admin_open")
async def cb_admin_open(c: CallbackQuery):
    if c.from_user.id != ADMIN_CHAT_ID:
        await c.answer("Только владелец бота", show_alert=True)
        return
    await safe_edit(c, "Админ-панель:", admin_kb()); await c.answer()

@dp.callback_query(F.data == "admin_toggle")
async def cb_admin_toggle(c: CallbackQuery):
    if c.from_user.id != ADMIN_CHAT_ID:
        return await c.answer("Нет доступа", show_alert=True)
    Store.accepting = not Store.accepting
    await safe_edit(c, f"Приём заявок: {'включен 🟢' if Store.accepting else 'выключен 🔴'}", admin_kb())
    await c.answer()

@dp.callback_query(F.data == "admin_stats")
async def cb_admin_stats(c: CallbackQuery):
    if c.from_user.id != ADMIN_CHAT_ID:
        return await c.answer("Нет доступа", show_alert=True)
    s = Store.stats
    txt = (f"📈 Статистика:\n"
           f"• Стартов: {s['starts']}\n"
           f"• Квиз (чат): {s['quiz']}\n"
           f"• Квиз (WebApp): {s['webquiz']}\n"
           f"• Заказы (контакт): {s['orders']}")
    await safe_edit(c, txt, admin_kb()); await c.answer()

@dp.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(c: CallbackQuery):
    if c.from_user.id != ADMIN_CHAT_ID:
        return await c.answer("Нет доступа", show_alert=True)
    await c.message.answer("Броадкаст-демо: рассылка пока не настроена.")
    await c.answer()

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

# --- Диагностика лида-чата ---
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

        def g(obj, attr, default="—"):
            return getattr(obj, attr, default)

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
    if m.from_user.id != ADMIN_CHAT_ID:
        return
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

# --- 🎁 Подарок ---
@dp.callback_query(F.data == "go_gift")
async def cb_gift(c: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📄 Чек-лист PDF", callback_data="gift_pdf"),
            InlineKeyboardButton(text="🎟 Промокод −20% (72ч)", callback_data="gift_promo"),
        ],
        [InlineKeyboardButton(text="⬅️ Меню", callback_data="go_menu")]
    ])
    text = (
        "<b>Выберите подарок</b>:\n"
        "• Чек-лист «Бот, который окупится за 48 часов» (1 стр.)\n"
        "• Промокод −20% на пакет Lite (действует 72 часа)"
    )
    await safe_edit(c, text, kb); await c.answer()

@dp.callback_query(F.data == "gift_pdf")
async def cb_gift_pdf(c: CallbackQuery):
    uid = c.from_user.id
    pdf_path = os.path.join(os.path.dirname(__file__), "assets", "gifts", "checklist.pdf")
    caption = (
        "<b>Чек-лист: «Бот, который окупится за 48 часов»</b>\n"
        "1) Цель бота = 1 метрика\n"
        "2) 4–6 кнопок, без перегруза\n"
        "3) УТП + hero-картинка\n"
        "4) Квиз 3–5 вопросов + 1 контакт\n"
        "5) Лиды → админ-чат / Sheets\n"
        "6) Автоответ клиенту\n"
        "7) Оффер с ограничением\n"
        "8) 3 кейса с цифрами\n"
        "9) Память бота (не допрос)\n"
        "10) Быстрые правки по данным\n"
        "11) Напоминания/рассылки\n"
        "12) Еженедельные цифры"
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
        "Скидка: −20% на пакет Lite\n"
        "Срок: до {exp}\n\n"
        "Как применить: укажите код при подтверждении заказа."
    ).format(code=esc(promo["code"]), exp=esc(promo["expires_utc"]))
    await c.message.answer(txt)
    await notify_admin(
        f"🎟 Промокод выдан: {c.from_user.full_name} (@{c.from_user.username or '—'}) → {promo['code']} до {promo['expires_utc']}"
    )
    Store.gift_claimed.add(uid)
    await c.answer()

# --- Fallback чат-квиз (если BASE_URL не задан) ---
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
    msg = (
        "🆕 Заявка (квиз-чат)\n"
        f"От: {ufmt(m)}\n"
        f"Ниша: {esc(data.get('niche'))}\n"
        f"Цель: {esc(data.get('goal'))}\n"
        f"Срок: {esc(data.get('deadline'))}\n"
        f"UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
    )
    await m.answer("Спасибо! Заявка получена 🎉", reply_markup=main_kb())
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

    txt = build_lead("WebApp", m, comp, task, contact)
    delivered = await notify_admin(txt)  # админу + лид-чат
    if delivered:
        ack = "Спасибо! Заявка отправлена ✅\n(доставлено в лид-чат и админу)"
    else:
        ack = "Спасибо! Заявка отправлена ✅\n⚠️ Лид-чат временно недоступен — админ уже уведомлён."
    await m.answer(ack, reply_markup=main_kb())

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
    await m.answer("Главное меню:", reply_markup=main_kb())
    await notify_admin(msg)

# --- Error handler (aiogram 3.7+: один аргумент event) ---
@dp.error()
async def on_error(event):
    exc = getattr(event, "exception", None)
    try:
        await notify_admin(f"⚠️ Ошибка: {html.escape(repr(exc))}")
    except Exception:
        pass
    logging.exception("Handler error: %s", exc)

# ---------- FASTAPI ----------
app = FastAPI(title="Vimly — Client Demo Bot (WebApp)")

# статика WebApp (html=True раздаёт index.html в папках)
STATIC_ROOT = os.path.join(os.path.dirname(__file__), "webapp")
if os.path.isdir(STATIC_ROOT):
    app.mount("/webapp", StaticFiles(directory=STATIC_ROOT, html=True), name="webapp")

# явный роут /webapp/quiz (+fallback встроенная форма)
FALLBACK_QUIZ_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Квиз-заявка</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,"Helvetica Neue",Arial;margin:0;padding:20px}
.card{max-width:640px;margin:0 auto;padding:20px;border:1px solid #eee;border-radius:16px}
label{display:block;margin:12px 0 6px;font-weight:600}
input,textarea{width:100%;padding:10px;border:1px solid #ccc;border-radius:10px}
button{margin-top:16px;padding:12px 16px;border:0;border-radius:12px;cursor:pointer}
button#send{background:#111;color:#fff}
</style></head><body>
<div class="card">
<h3>Квиз-заявка</h3>
<label>Описание компании</label>
<textarea id="company" rows="3" placeholder="Чем занимаетесь?"></textarea>
<label>Задача</label>
<textarea id="task" rows="3" placeholder="Что нужно сделать боту?"></textarea>
<label>Контакт в Telegram</label>
<input id="contact" placeholder="@username или телефон">
<button id="send">Отправить</button>
</div>
<script>
(function(){
  const tg = window.Telegram && Telegram.WebApp ? Telegram.WebApp : null;
  const btn = document.getElementById('send');
  function send(){
    const payload = {
      company: document.getElementById('company').value||"",
      task: document.getElementById('task').value||"",
      contact: document.getElementById('contact').value||""
    };
    if (tg && tg.sendData){
      tg.sendData(JSON.stringify(payload));
      tg.close();
    } else {
      alert('Откройте форму из бота, через кнопку «Квиз-заявка».');
    }
  }
  if (tg){ tg.expand(); tg.ready(); }
  btn.addEventListener('click', send);
})();
</script>
</body></html>"""

@app.get("/webapp/quiz", response_class=HTMLResponse)
@app.get("/webapp/quiz/", response_class=HTMLResponse)
async def webapp_quiz():
    index_path = os.path.join(STATIC_ROOT, "quiz", "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, media_type="text/html")
    return HTMLResponse(FALLBACK_QUIZ_HTML)

# фавикон (чтобы не было 404)
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    hero = os.path.join(os.path.dirname(__file__), "assets", "hero.png")
    if os.path.exists(hero):
        return FileResponse(hero, media_type="image/png")
    return Response(status_code=204)

# HEAD-хендлеры (убирают 405 от пингов Render)
@app.head("/")
async def head_root():
    return Response(status_code=200)

@app.head("/healthz")
async def head_healthz():
    return Response(status_code=200)

@app.get("/", response_class=HTMLResponse)
async def index():
    return f"<h3>{esc(BRAND_NAME)} — {esc(BRAND_TAGLINE)}</h3>"

@app.get("/healthz", response_class=PlainTextResponse)
async def healthz():
    return "ok"

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

# ---------- LIFECYCLE ----------
@app.on_event("startup")
async def on_startup():
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
