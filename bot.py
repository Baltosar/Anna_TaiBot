import asyncio
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, time as dtime
from typing import Dict, List, Optional, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ForceReply,
)

# Local modules in your project
import booking as booking_mod
from ai import ai_reply

# booking.py compatibility
# Different versions of booking.py expose different helper names.
# We normalize to two call sites:
#   - create_booking(name, phone, service_name, date, time) -> link
#   - is_time_available(date_str, time_str, duration_min=60) -> bool
create_booking = getattr(booking_mod, "create_booking")


def create_booking_compat(
    *,
    client_name: str,
    phone: str,
    service_name: str,
    date_str: str,
    time_str: str,
    duration_min: int = 60,
    comment: str = "",
):
    """Call booking.create_booking with whatever signature is implemented in booking.py.

    Your booking.py currently expects:
        create_booking(date_str, time_str, service_name, client_name, phone, duration_minutes=..., comment=...)
    But older bot versions called it with different keyword names.
    """

    # First: try the expected new signature (booking_v2.py / current booking.py)
    try:
        return create_booking(
            date_str=date_str,
            time_str=time_str,
            service_name=service_name,
            client_name=client_name,
            phone=phone,
            duration_minutes=int(duration_min),
            comment=comment or "",
        )
    except TypeError:
        pass

    # Fallbacks for older signatures (just in case)
    try:
        return create_booking(
            date=date_str,
            time=time_str,
            service=service_name,
            name=client_name,
            phone=phone,
            duration=int(duration_min),
            comment=comment or "",
        )
    except TypeError:
        # Last resort: positional
        return create_booking(date_str, time_str, service_name, client_name, phone, int(duration_min), comment or "")


def is_time_available(date_str: str, time_str: str, duration_min: int = 60) -> bool:
    if hasattr(booking_mod, "check_slot_available"):
        return bool(booking_mod.check_slot_available(date_str=date_str, time_str=time_str, duration_minutes=duration_min))
    if hasattr(booking_mod, "is_time_available"):
        # legacy signature
        return bool(booking_mod.is_time_available(date_str=date_str, time_str=time_str))
    # Fallback: if no availability checker exists, allow and rely on create_booking()
    return True


# -------------------------
# Config
# -------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # used inside ai.py
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID") or os.getenv("GOOGLE_CALENDAR_ID")

# IMPORTANT: multi-admins
# Railway variable: ADMIN_CHAT_IDS="7386535618,1676430828" (comma-separated)
ADMIN_CHAT_IDS_RAW = os.getenv("ADMIN_CHAT_IDS") or os.getenv("ADMIN_CHAT_ID")

# Timezone: Moscow by default
TZ_NAME = os.getenv("BOT_TIMEZONE", "Europe/Moscow")

# Working hours for slot suggestions
WORK_START_HOUR = int(os.getenv("WORK_START_HOUR", "10"))
WORK_END_HOUR = int(os.getenv("WORK_END_HOUR", "20"))
SLOT_STEP_MIN = int(os.getenv("SLOT_STEP_MIN", "30"))
DEFAULT_DURATION_MIN = int(os.getenv("DEFAULT_DURATION_MIN", "60"))

# Logging
LOG_FILE = os.getenv("BOOKINGS_LOG", "bookings.log")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")
if not ADMIN_CHAT_IDS_RAW:
    raise RuntimeError("ADMIN_CHAT_IDS (or ADMIN_CHAT_ID) not set")

try:
    ADMIN_CHAT_IDS: List[int] = [int(x.strip()) for x in ADMIN_CHAT_IDS_RAW.split(",") if x.strip()]
except Exception as e:
    raise RuntimeError("ADMIN_CHAT_IDS must be comma-separated integers") from e

if not ADMIN_CHAT_IDS:
    raise RuntimeError("ADMIN_CHAT_IDS is empty")

# -------------------------
# Time helpers
# -------------------------
try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo(TZ_NAME)
except Exception:
    TZ = None


def now_local() -> datetime:
    if TZ is None:
        return datetime.now()
    return datetime.now(tz=TZ)


def parse_date_time_ru(text: str, *, reference: Optional[datetime] = None) -> Optional[Tuple[str, str]]:
    """Try to extract (date_str YYYY-MM-DD, time_str HH:MM) from free text in Russian.

    Supports:
      - "2026-01-17 17:00"
      - "17.01 17:00" (assumes current year)
      - "сегодня 10:00", "завтра 18:30"
      - "17:00" (assumes today)

    Returns None if can't.
    """
    if reference is None:
        reference = now_local()

    t = text.strip().lower()

    # 1) ISO-like date
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})", t)
    if m:
        y, mo, d, hh, mm = map(int, m.groups())
        try:
            dt = datetime(y, mo, d, hh, mm, tzinfo=reference.tzinfo)
            return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
        except ValueError:
            return None

    # 2) dd.mm[.yyyy] + time
    m = re.search(r"(\d{1,2})[\./-](\d{1,2})(?:[\./-](\d{2,4}))?\s+(\d{1,2}):(\d{2})", t)
    if m:
        d, mo, y_raw, hh, mm = m.groups()
        d = int(d)
        mo = int(mo)
        hh = int(hh)
        mm = int(mm)
        if y_raw:
            y = int(y_raw)
            if y < 100:
                y += 2000
        else:
            y = reference.year
        try:
            dt = datetime(y, mo, d, hh, mm, tzinfo=reference.tzinfo)
            return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
        except ValueError:
            return None

    # 3) today/tomorrow + time
    m = re.search(r"\b(сегодня|завтра)\b[^\d]*(\d{1,2}):(\d{2})", t)
    if m:
        day_word, hh, mm = m.groups()
        hh = int(hh)
        mm = int(mm)
        base = reference.date()
        if day_word == "завтра":
            base = (reference + timedelta(days=1)).date()
        try:
            dt = datetime.combine(base, dtime(hh, mm), tzinfo=reference.tzinfo)
            return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
        except ValueError:
            return None

    # 4) time only -> today
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", t)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2))
        try:
            dt = datetime.combine(reference.date(), dtime(hh, mm), tzinfo=reference.tzinfo)
            return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
        except ValueError:
            return None

    return None


def is_future_slot(date_str: str, time_str: str, *, grace_minutes: int = 0) -> bool:
    """True if slot is strictly in the future (with optional grace).
    grace_minutes=5 means we treat slots earlier than now+5 as past.
    """
    ref = now_local()
    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        if ref.tzinfo is not None:
            dt = dt.replace(tzinfo=ref.tzinfo)
    except Exception:
        return False
    return dt > (ref + timedelta(minutes=grace_minutes))


def suggest_slots(
    *,
    days_ahead: int = 7,
    limit: int = 6,
    duration_minutes: int = DEFAULT_DURATION_MIN,
) -> List[Tuple[str, str]]:
    """Suggest next free slots starting from now."""
    suggestions: List[Tuple[str, str]] = []
    start = now_local()

    for day_offset in range(0, days_ahead + 1):
        day = (start + timedelta(days=day_offset)).date()

        # start time for the day
        if day_offset == 0:
            first_minutes = ((start.minute // SLOT_STEP_MIN) + 1) * SLOT_STEP_MIN
            cur = start.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=first_minutes)
        else:
            cur = datetime.combine(day, dtime(WORK_START_HOUR, 0), tzinfo=start.tzinfo)

        end = datetime.combine(day, dtime(WORK_END_HOUR, 0), tzinfo=start.tzinfo)

        while cur < end:
            date_str = cur.strftime("%Y-%m-%d")
            time_str = cur.strftime("%H:%M")
            if is_future_slot(date_str, time_str, grace_minutes=0) and is_time_available(date_str, time_str):
                suggestions.append((date_str, time_str))
                if len(suggestions) >= limit:
                    return suggestions
            cur += timedelta(minutes=SLOT_STEP_MIN)

    return suggestions


def format_slots(slots: List[Tuple[str, str]]) -> str:
    if not slots:
        return "(пока нет свободных слотов)"
    out = []
    for ds, ts in slots:
        try:
            dt = datetime.strptime(f"{ds} {ts}", "%Y-%m-%d %H:%M")
            out.append(dt.strftime("%d.%m %H:%M"))
        except Exception:
            out.append(f"{ds} {ts}")
    return ", ".join(out)


# -------------------------
# Data models
# -------------------------
@dataclass
class PendingRequest:
    req_id: str
    user_id: int
    chat_id: int
    created_at: str
    service_name: str
    client_name: str
    phone: str
    date_str: str
    time_str: str
    duration_min: int
    comment: str = ""
    status: str = "PENDING"  # PENDING/CONFIRMED/CANCELLED
    confirmed_by: Optional[int] = None


PENDING: Dict[str, PendingRequest] = {}

# live admin handoff: user_id -> admin_id
LIVE_ADMIN: Dict[int, int] = {}

# forwarded admin message map: (admin_id, msg_id) -> user_chat_id
FORWARDED_MAP: Dict[Tuple[int, int], int] = {}


def admin_chat_kb(client_chat_id: int) -> InlineKeyboardMarkup:
    """Inline keyboard shown to admins under each client message."""

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✍️ Ответить", callback_data=f"admin:replyto:{client_chat_id}"
                ),
                InlineKeyboardButton(
                    text="✅ Завершить чат", callback_data=f"admin:endchat:{client_chat_id}"
                ),
            ]
        ]
    )


# round-robin admin selection
_admin_rr_idx = 0


def pick_admin() -> int:
    global _admin_rr_idx
    admin = ADMIN_CHAT_IDS[_admin_rr_idx % len(ADMIN_CHAT_IDS)]
    _admin_rr_idx += 1
    return admin


def log_event(event: str, payload: dict) -> None:
    logging.getLogger("bookings").info("%s %s", event, json.dumps(payload, ensure_ascii=False))


def kb_client() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Записаться", callback_data="client:book")],
            [InlineKeyboardButton(text="👩‍💼 Администратор", callback_data="client:admin")],
        ]
    )


def kb_client_live_admin() -> InlineKeyboardMarkup:
    """Client keyboard while in live-admin mode."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Завершить чат", callback_data="client:endchat")],
            [InlineKeyboardButton(text="📅 Записаться", callback_data="client:book")],
        ]
    )


def kb_admin_actions(req_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"admin:confirm:{req_id}"),
                InlineKeyboardButton(text="❌ Отменить", callback_data=f"admin:cancel:{req_id}"),
            ]
        ]
    )


# -------------------------
# FSM
# -------------------------
class BookingFSM(StatesGroup):
    service = State()
    name = State()
    phone = State()
    datetime = State()
    comment = State()


class AdminReplyFSM(StatesGroup):
    waiting_text = State()


# -------------------------
# Bot init
# -------------------------
bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

storage = MemoryStorage()
dp = Dispatcher(storage=storage)


# -------------------------
# Handlers
# -------------------------
@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "Привет! Я помогу записаться на массаж.\n\n"
        "Нажмите кнопку <b>Записаться</b> или просто напишите дату и время (например: «завтра 18:30 тайский массаж»).",
        reply_markup=kb_client(),
    )


@dp.message(Command("book"))
async def cmd_book(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(BookingFSM.service)
    await message.answer(
        "На какую услугу записать? Например: <i>тайский массаж</i>",
        reply_markup=kb_client(),
    )


@dp.callback_query(F.data == "client:book")
async def cb_book(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await state.set_state(BookingFSM.service)
    await callback.message.answer("На какую услугу записать? Например: <i>тайский массаж</i>")


@dp.callback_query(F.data == "client:admin")
async def cb_admin(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    admin_id = pick_admin()
    LIVE_ADMIN[user_id] = admin_id

    # notify admins
    for aid in ADMIN_CHAT_IDS:
        try:
            msg = await bot.send_message(
                aid,
                (
                    f"📨 Клиент просит администратора.\n"
                    f"User: <code>{user_id}</code>\n"
                    f"Chat: <code>{callback.message.chat.id}</code>\n"
                    f"Вы назначены: {'✅' if aid == admin_id else '—'}\n\n"
                    f"Нажмите <b>✍️ Ответить</b> ниже и напишите сообщение — я отправлю клиенту.\n"
                    f"(Можно также <i>ответить реплаем</i> на любое сообщение клиента.)"
                ),
                reply_markup=admin_chat_kb(callback.message.chat.id),
            )
            FORWARDED_MAP[(aid, msg.message_id)] = callback.message.chat.id
        except Exception:
            pass

    await callback.message.answer(
        "Хорошо, подключаю администратора. Пишите ваш вопрос — я передам.\n\n"
        "Чтобы закончить чат, нажмите «✅ Завершить чат». После завершения я (AI) снова буду отвечать автоматически.",
        reply_markup=kb_client_live_admin(),
    )


@dp.message(BookingFSM.service)
async def fsm_service(message: Message, state: FSMContext):
    await state.update_data(service_name=message.text.strip())
    await state.set_state(BookingFSM.name)
    await message.answer("Как вас зовут?")


@dp.message(BookingFSM.name)
async def fsm_name(message: Message, state: FSMContext):
    await state.update_data(client_name=message.text.strip())
    await state.set_state(BookingFSM.phone)
    await message.answer("Номер телефона? (можно в любом формате)")


@dp.message(BookingFSM.phone)
async def fsm_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text.strip())
    await state.set_state(BookingFSM.datetime)

    slots = suggest_slots(limit=4)
    await message.answer(
        "Когда вам удобно?\n"
        "Пример: <code>17.01 18:30</code> или <code>завтра 18:30</code>\n\n"
        f"Ближайшие свободные слоты: {format_slots(slots)}",
    )


@dp.message(BookingFSM.datetime)
async def fsm_datetime(message: Message, state: FSMContext):
    parsed = parse_date_time_ru(message.text)
    if not parsed:
        slots = suggest_slots(limit=6)
        await message.answer(
            "Не понял дату/время.\n"
            "Напишите, например: <code>17.01 18:30</code> или <code>завтра 18:30</code>.\n\n"
            f"Ближайшие свободные слоты: {format_slots(slots)}"
        )
        return

    date_str, time_str = parsed

    # past protection: forbid if already started (grace 0) - user asked "10:05" should forbid "10:00"
    if not is_future_slot(date_str, time_str, grace_minutes=0):
        slots = suggest_slots(limit=6)
        await message.answer(
            "Это время уже прошло.\n\n"
            f"Ближайшие свободные слоты: {format_slots(slots)}"
        )
        return

    if not is_time_available(date_str, time_str):
        slots = suggest_slots(limit=6)
        await message.answer(
            "К сожалению, этот слот занят.\n\n"
            f"Ближайшие свободные слоты: {format_slots(slots)}"
        )
        return

    await state.update_data(date_str=date_str, time_str=time_str)
    await state.set_state(BookingFSM.comment)
    await message.answer("Комментарий для администратора? (если нет — напишите <code>-</code>)")


@dp.message(BookingFSM.comment)
async def fsm_comment(message: Message, state: FSMContext):
    data = await state.get_data()
    comment = message.text.strip()
    if comment == "-":
        comment = ""

    req = PendingRequest(
        req_id=secrets.token_hex(5),
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        created_at=now_local().isoformat(),
        service_name=data.get("service_name", ""),
        client_name=data.get("client_name", ""),
        phone=data.get("phone", ""),
        date_str=data.get("date_str", ""),
        time_str=data.get("time_str", ""),
        duration_min=DEFAULT_DURATION_MIN,
        comment=comment,
    )

    PENDING[req.req_id] = req
    log_event("NEW_PENDING", asdict(req))

    text = (
        "🆕 <b>Новая заявка</b>\n"
        f"ID: <code>{req.req_id}</code>\n"
        f"Клиент: <b>{req.client_name}</b>\n"
        f"Тел: <code>{req.phone}</code>\n"
        f"Услуга: <b>{req.service_name}</b>\n"
        f"Когда: <b>{req.date_str} {req.time_str}</b>\n"
        + (f"Комментарий: {req.comment}\n" if req.comment else "")
        + f"User: <code>{req.user_id}</code>"
    )

    # Send to all admins
    for aid in ADMIN_CHAT_IDS:
        try:
            msg = await bot.send_message(aid, text, reply_markup=kb_admin_actions(req.req_id))
            FORWARDED_MAP[(aid, msg.message_id)] = req.chat_id
        except Exception:
            pass

    await message.answer(
        "Спасибо! Заявка отправлена администратору на подтверждение.\n"
        "Как только подтвердят — пришлю сообщение.",
        reply_markup=kb_client(),
    )
    await state.clear()


# -------------------------
# Admin callbacks
# -------------------------
@dp.callback_query(F.data.startswith("admin:confirm:"))
async def admin_confirm(callback: CallbackQuery):
    await callback.answer()
    admin_id = callback.from_user.id

    req_id = callback.data.split(":", 2)[2]
    req = PENDING.get(req_id)
    if not req:
        await callback.message.edit_text("Заявка не найдена или уже обработана.")
        return

    if req.status != "PENDING":
        await callback.message.edit_text(f"Заявка уже обработана: {req.status}")
        return

    # Re-check availability (race protection)
    if not is_future_slot(req.date_str, req.time_str, grace_minutes=0):
        req.status = "CANCELLED"
        req.confirmed_by = admin_id
        log_event("CANCELLED_PAST", asdict(req))
        await callback.message.edit_text("Нельзя подтвердить: время уже прошло.")
        await bot.send_message(req.chat_id, "Увы, этот слот уже прошёл. Пожалуйста, выберите другое время.")
        return

    if not is_time_available(req.date_str, req.time_str):
        slots = suggest_slots(limit=6)
        await callback.message.edit_text("Нельзя подтвердить: слот уже занят.")
        await bot.send_message(
            req.chat_id,
            "Увы, это время уже занято.\n\n"
            f"Ближайшие свободные слоты: {format_slots(slots)}",
            reply_markup=kb_client(),
        )
        return

    # Create calendar event (signature differs between revisions of booking.py)
    link = create_booking_compat(
        client_name=req.client_name,
        phone=req.phone,
        service_name=req.service_name,
        date_str=req.date_str,
        time_str=req.time_str,
        duration_min=req.duration_min,
        comment=req.comment,
    )

    req.status = "CONFIRMED"
    req.confirmed_by = admin_id
    log_event("CONFIRMED", {**asdict(req), "link": link, "admin": admin_id})

    # Notify user
    await bot.send_message(
        req.chat_id,
        "✅ Запись подтверждена!\n"
        f"<b>{req.service_name}</b>\n"
        f"Когда: <b>{req.date_str} {req.time_str}</b>\n"
        f"Ссылка на событие: {link}",
        reply_markup=kb_client(),
    )

    # Notify all admins
    for aid in ADMIN_CHAT_IDS:
        try:
            await bot.send_message(
                aid,
                f"✅ Подтверждено админом <code>{admin_id}</code>\n"
                f"ID: <code>{req.req_id}</code>\n"
                f"Клиент: {req.client_name} ({req.phone})\n"
                f"Когда: {req.date_str} {req.time_str}\n"
                f"Ссылка: {link}",
            )
        except Exception:
            pass

    await callback.message.edit_text("✅ Подтверждено.")


@dp.callback_query(F.data.startswith("admin:cancel:"))
async def admin_cancel(callback: CallbackQuery):
    await callback.answer()
    admin_id = callback.from_user.id

    req_id = callback.data.split(":", 2)[2]
    req = PENDING.get(req_id)
    if not req:
        await callback.message.edit_text("Заявка не найдена или уже обработана.")
        return

    if req.status != "PENDING":
        await callback.message.edit_text(f"Заявка уже обработана: {req.status}")
        return

    req.status = "CANCELLED"
    req.confirmed_by = admin_id
    log_event("CANCELLED", asdict(req))

    await bot.send_message(
        req.chat_id,
        "❌ Запись отменена администратором.\n"
        "Если хотите — выберите другое время.",
        reply_markup=kb_client(),
    )
    await callback.message.edit_text("❌ Отменено.")


# -------------------------
# Live admin handoff (messages)
# -------------------------
@dp.message(Command("ai"))
async def cmd_ai(message: Message):
    user_id = message.from_user.id
    if user_id in LIVE_ADMIN:
        LIVE_ADMIN.pop(user_id, None)
        await message.answer("Ок, возвращаюсь в режим AI-администратора.", reply_markup=kb_client())
    else:
        await message.answer("Вы уже в режиме AI.", reply_markup=kb_client())


@dp.message(
    F.reply_to_message,
    F.from_user.id.in_(ADMIN_CHAT_IDS)
)
async def admin_reply_to_forward(message: Message):
    replied = message.reply_to_message

    # ⚠️ Если это НЕ ответ на пересланное сообщение клиента — просто выходим
    if not replied or replied.message_id not in FORWARDED_MAP:
        return

    target_chat_id = FORWARDED_MAP[replied.message_id]

    text = (message.text or "").strip()
    if not text:
        return

    try:
        await bot.send_message(
            target_chat_id,
            f"👩‍💼 Администратор:\n{text}"
        )
        await message.answer("✅ Сообщение отправлено клиенту.")
    except Exception:
        await message.answer("❗ Не удалось отправить сообщение клиенту.")



@dp.callback_query(F.data.startswith("admin:replyto:"))
async def admin_pick_chat(callback: CallbackQuery, state: FSMContext):
    """Let admin select a client chat to reply to (without requiring Reply-to)."""

    if callback.from_user.id not in ADMIN_CHAT_IDS:
        await callback.answer()
        return

    try:
        chat_id = int((callback.data or "").split(":")[-1])
    except Exception:
        await callback.answer("Не смог определить чат.")
        return

    await state.set_state(AdminReplyFSM.waiting_text)
    await state.update_data(target_chat_id=chat_id)
    await callback.answer("Ок")
    await callback.message.answer(
        f"✍️ Напишите ответ клиенту (chat_id: <code>{chat_id}</code>).\n"
        f"Чтобы закончить, отправьте /end.",
        reply_markup=ForceReply(selective=True),
    )


@dp.message(Command("end"), F.from_user.id.in_(ADMIN_CHAT_IDS))
async def admin_end_session(message: Message, state: FSMContext):
    """Admin ends the current reply session (and also closes live chat for that client, if active)."""
    data = await state.get_data()
    target_chat_id = data.get("target_chat_id")
    await state.clear()

    # If this admin was chatting live with a client, close it.
    if isinstance(target_chat_id, int) and LIVE_ADMIN.get(target_chat_id) == message.from_user.id:
        LIVE_ADMIN.pop(target_chat_id, None)
        try:
            await bot.send_message(
                target_chat_id,
                "✅ Чат с администратором завершён. Я снова на связи (AI) — можете продолжить диалог или записаться.",
                reply_markup=kb_client(),
            )
        except Exception:
            pass

    await message.answer("✅ Диалог завершён.")


@dp.message(Command("end"))
async def client_end_session(message: Message, state: FSMContext):
    """Client ends live-admin chat and returns to AI."""
    user_id = message.from_user.id
    if user_id not in LIVE_ADMIN:
        return

    admin_id = LIVE_ADMIN.pop(user_id, None)
    await state.clear()

    await message.answer(
        "✅ Чат с администратором завершён. Я снова на связи (AI) — можете продолжить диалог или записаться.",
        reply_markup=kb_client(),
    )

    if admin_id:
        try:
            await bot.send_message(admin_id, f"✅ Клиент {user_id} завершил чат.")
        except Exception:
            pass



@dp.callback_query(F.data.startswith("admin:endchat:"))
async def cb_admin_end_chat(callback: CallbackQuery, state: FSMContext):
    """Finish live admin chat for a specific client (button ✅ Завершить чат)."""
    if callback.from_user.id not in ADMIN_CHAT_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        client_chat_id = int(callback.data.split(":", 2)[2])
    except Exception:
        await callback.answer("Ошибка данных", show_alert=True)
        return

    # Close live-admin mode
    removed_admin = LIVE_ADMIN.pop(client_chat_id, None)

    # UI feedback
    await callback.answer("Чат завершён")

    # If this admin was in reply-mode, exit it (so next messages don't get stuck)
    try:
        if await state.get_state() == AdminReplyFSM.waiting_text.state:
            await state.clear()
    except Exception:
        # best-effort; don't block chat closing
        pass

    # Notify client: AI is back automatically because LIVE_ADMIN entry is removed
    try:
        await bot.send_message(
            client_chat_id,
            "✅ Чат с администратором завершён.\n\nТеперь снова отвечает AI-ассистент — можете писать вопрос или /book для записи.",
        )
    except Exception:
        pass

    # Notify admins (including who ended it)
    note = (
        f"✅ Завершён чат с клиентом {client_chat_id}. "
        f"Завершил админ {callback.from_user.id}."
    )
    for admin_id in ADMIN_CHAT_IDS:
        try:
            await bot.send_message(admin_id, note)
        except Exception:
            pass

    # Try to remove buttons from the message where it was pressed
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@dp.callback_query(F.data == "client:endchat")
async def cb_client_end_chat(callback: CallbackQuery, state: FSMContext):
    """Client ends live-admin chat via button and returns to AI."""
    if not callback.from_user:
        return

    user_id = callback.from_user.id
    if user_id not in LIVE_ADMIN:
        await callback.answer("Чат уже завершён", show_alert=False)
        return

    admin_id = LIVE_ADMIN.pop(user_id, None)
    await state.clear()
    await callback.answer("Чат завершён")

    # Notify client
    try:
        await callback.message.answer(
            "✅ Чат с администратором завершён. Я снова на связи (AI) — можете продолжить диалог или записаться.",
            reply_markup=kb_client(),
        )
    except Exception:
        pass

    # Notify admin (best-effort)
    if admin_id:
        try:
            await bot.send_message(admin_id, f"✅ Клиент {user_id} завершил чат.")
        except Exception:
            pass


@dp.message(AdminReplyFSM.waiting_text)
async def admin_send_to_client(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_CHAT_IDS:
        return

    data = await state.get_data()
    chat_id = data.get("target_chat_id")
    if not chat_id:
        await message.answer("❗ Не выбран клиент. Нажмите «✍️ Ответить» под сообщением клиента.")
        await state.clear()
        return

    text = (message.text or "").strip()
    if not text:
        return

    try:
        await bot.send_message(int(chat_id), f"👩‍💼 Администратор: {text}")
        await message.answer("✅ Отправлено клиенту.")
    except Exception:
        await message.answer("❗ Не смог отправить клиенту. Возможно, он заблокировал бота.")


# -------------------------
# Main chat handler (AI + date/time detection + live admin)
# -------------------------
@dp.message()
async def handle_message(message: Message, state: FSMContext):
    # If user is in FSM, other handlers should catch.
    cur_state = await state.get_state()
    if cur_state is not None:
        return

    user_id = message.from_user.id
    text = (message.text or "").strip()

    # If live admin mode -> forward to selected admin(s)
    if user_id in LIVE_ADMIN:
        admin_id = LIVE_ADMIN[user_id]
        for aid in ADMIN_CHAT_IDS:
            try:
                prefix = "✅ назначен" if aid == admin_id else ""
                msg = await bot.send_message(
                    aid,
                    f"💬 Сообщение от клиента {prefix}\nUser: <code>{user_id}</code>\n\n{text}",
                    reply_markup=admin_chat_kb(message.chat.id),
                )
                FORWARDED_MAP[(aid, msg.message_id)] = message.chat.id
            except Exception:
                pass
        await message.answer("Передала администратору. Он ответит вам здесь.")
        return

    # If message looks like booking intent with date/time -> start quick booking
    parsed = parse_date_time_ru(text)
    if parsed:
        date_str, time_str = parsed

        if not is_future_slot(date_str, time_str, grace_minutes=0):
            slots = suggest_slots(limit=6)
            await message.answer(
                "Это время уже прошло.\n\n"
                f"Ближайшие свободные слоты: {format_slots(slots)}\n\n"
                "Чтобы записаться, нажмите «Записаться» или напишите /book.",
                reply_markup=kb_client(),
            )
            return

        if not is_time_available(date_str, time_str):
            slots = suggest_slots(limit=6)
            await message.answer(
                "Этот слот занят.\n\n"
                f"Ближайшие свободные слоты: {format_slots(slots)}\n\n"
                "Хотите записаться? Нажмите «Записаться» или /book.",
                reply_markup=kb_client(),
            )
            return

        # We have a free future slot; move user to /book FSM with prefilled date/time
        await state.clear()
        await state.set_state(BookingFSM.service)
        await state.update_data(date_str=date_str, time_str=time_str)
        await message.answer(
            f"Ок! Вижу свободное время <b>{date_str} {time_str}</b>.\n"
            "Давайте оформим запись — какая услуга?",
            reply_markup=kb_client(),
        )
        return

    # Otherwise: AI admin response
    history = [
        {
            "role": "system",
            "content": (
                "Ты вежливый AI-администратор массажного салона. "
                "Твоя цель — помочь клиенту с вопросами и при необходимости записать. "
                "Если клиент хочет запись, попроси дату и время, услугу, имя и телефон. "
                "Если клиент спрашивает про свободные слоты — предложи несколько ближайших."
            ),
        },
        {"role": "user", "content": text},
    ]

    try:
        reply = await ai_reply(history)
    except Exception:
        reply = (
            "Я на связи. Чтобы записаться, нажмите «Записаться» или напишите /book. "
            "Также можно написать дату и время, например: «завтра 18:30 тайский массаж»."
        )

    # If AI asks for slots, proactively append actual slot list
    if re.search(r"слот|свободн|время", reply.lower()):
        slots = suggest_slots(limit=6)
        reply = reply.rstrip() + "\n\nБлижайшие свободные слоты: " + format_slots(slots)

    await message.answer(reply, reply_markup=kb_client())


# -------------------------
# Logging setup & run
# -------------------------
def setup_logging() -> None:
    os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Console
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    root.addHandler(ch)

    # bookings log
    bl = logging.getLogger("bookings")
    bl.setLevel(logging.INFO)
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    bl.addHandler(fh)


async def main() -> None:
    setup_logging()
    logging.info("Start polling")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
