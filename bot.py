# bot.py
import os
import re
import uuid
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, date as date_cls
from zoneinfo import ZoneInfo
from typing import Optional, Tuple, Dict, List

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from booking import create_booking, is_time_available


# -----------------------------
# Config
# -----------------------------
TZ = ZoneInfo("Europe/Moscow")

BOT_TOKEN = os.getenv("BOT_TOKEN")

# Prefer ADMIN_CHAT_IDS (comma-separated). Fallback to ADMIN_CHAT_ID (single).
_admin_ids_raw = os.getenv("ADMIN_CHAT_IDS") or os.getenv("ADMIN_CHAT_ID") or ""
ADMIN_IDS: List[int] = []
if _admin_ids_raw.strip():
    # allow separators: comma/space
    parts = re.split(r"[,\s]+", _admin_ids_raw.strip())
    ADMIN_IDS = [int(p) for p in parts if p.strip()]

GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")  # used inside booking.py typically

WORK_START_HOUR = int(os.getenv("WORK_START_HOUR", "10"))  # 10:00
WORK_END_HOUR = int(os.getenv("WORK_END_HOUR", "21"))      # 21:00 (last start depends on duration)
SLOT_STEP_MIN = int(os.getenv("SLOT_STEP_MIN", "30"))      # 30 min
DEFAULT_DURATION_MIN = int(os.getenv("DEFAULT_DURATION_MIN", "60"))
PAST_GRACE_MIN = int(os.getenv("PAST_GRACE_MIN", "0"))     # 0 => "сегодня 10:00" at 10:05 is forbidden

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

if not ADMIN_IDS:
    raise RuntimeError("ADMIN_CHAT_IDS/ADMIN_CHAT_ID not set")

# -----------------------------
# Logging
# -----------------------------
logger = logging.getLogger("booking-bot")
logger.setLevel(logging.INFO)

fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
# File log (Railway persists only if volume; but you already created file)
file_handler = logging.FileHandler("bookings.log", encoding="utf-8")
file_handler.setFormatter(fmt)
file_handler.setLevel(logging.INFO)

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(fmt)
stream_handler.setLevel(logging.INFO)

# Avoid duplicate handlers on hot reload
if not logger.handlers:
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

# -----------------------------
# Models / storage
# -----------------------------
@dataclass
class PendingRequest:
    req_id: str
    user_id: int
    chat_id: int
    created_at: str  # iso
    service_name: str
    client_name: str
    phone: str
    date_str: str  # YYYY-MM-DD
    time_str: str  # HH:MM
    duration_min: int = DEFAULT_DURATION_MIN
    comment: str = ""


PENDING: Dict[str, PendingRequest] = {}


# -----------------------------
# FSM
# -----------------------------
class BookingFSM(StatesGroup):
    service = State()
    date = State()
    time = State()
    name = State()
    phone = State()
    comment = State()


# -----------------------------
# Helpers: parsing and validation
# -----------------------------
DATE_RE_YMD = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
DATE_RE_DMY = re.compile(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\b")
TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")

MONTHS_RU = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5,
    "июн": 6, "июл": 7, "август": 8, "сентябр": 9, "октябр": 10,
    "ноябр": 11, "декабр": 12,
}

def now_local() -> datetime:
    return datetime.now(TZ)

def _format_date(d: date_cls) -> str:
    return d.strftime("%Y-%m-%d")

def _format_time(dt: datetime) -> str:
    return dt.strftime("%H:%M")

def parse_date_time_from_text(text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Very simple RU-friendly parser.
    Returns (date_str YYYY-MM-DD, time_str HH:MM) or (None, None) if not found.
    Supports:
      - "2026-01-15 18:30"
      - "15.01.2026 18:30"
      - "сегодня 10:00", "завтра 18:30", "послезавтра 12:00"
      - "5 января 10:00" (month in Russian, any suffix)
    """
    t = (text or "").strip().lower()

    # time
    tm = TIME_RE.search(t)
    time_str = None
    if tm:
        hh = int(tm.group(1))
        mm = int(tm.group(2))
        time_str = f"{hh:02d}:{mm:02d}"

    # explicit YYYY-MM-DD
    m = DATE_RE_YMD.search(t)
    if m:
        y, mo, d = map(int, m.groups())
        try:
            date_str = datetime(y, mo, d).date().strftime("%Y-%m-%d")
            return date_str, time_str
        except ValueError:
            return None, time_str

    # explicit DD.MM.YYYY
    m = DATE_RE_DMY.search(t)
    if m:
        d, mo, y = map(int, m.groups())
        try:
            date_str = datetime(y, mo, d).date().strftime("%Y-%m-%d")
            return date_str, time_str
        except ValueError:
            return None, time_str

    # relative words
    base = now_local().date()
    if "послезавтра" in t:
        date_str = _format_date(base + timedelta(days=2))
        return date_str, time_str
    if "завтра" in t:
        date_str = _format_date(base + timedelta(days=1))
        return date_str, time_str
    if "сегодня" in t:
        date_str = _format_date(base)
        return date_str, time_str

    # "5 января"
    # find day + month word
    dm = re.search(r"\b(\d{1,2})\s+([а-яё]+)\b", t)
    if dm:
        day = int(dm.group(1))
        mon_word = dm.group(2)
        mon = None
        for k, v in MONTHS_RU.items():
            if mon_word.startswith(k):
                mon = v
                break
        if mon:
            y = now_local().year
            # if month already passed and user likely means next year, bump year
            try:
                dt_candidate = datetime(y, mon, day).date()
                if dt_candidate < now_local().date():
                    dt_candidate = datetime(y + 1, mon, day).date()
                return _format_date(dt_candidate), time_str
            except ValueError:
                return None, time_str

    return None, time_str

def local_dt(date_str: str, time_str: str) -> datetime:
    naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    return naive.replace(tzinfo=TZ)

def is_future_slot(date_str: str, time_str: str) -> bool:
    dt = local_dt(date_str, time_str)
    return dt > (now_local() + timedelta(minutes=PAST_GRACE_MIN))

def suggest_next_slots(
    service_name: str,
    start_from: Optional[datetime] = None,
    days_ahead: int = 7,
    limit: int = 6,
    duration_min: int = DEFAULT_DURATION_MIN
) -> List[Tuple[str, str]]:
    """
    Suggest next available slots (date_str, time_str) within days_ahead.
    Checks future-only and Google Calendar availability via booking.is_time_available.
    """
    if start_from is None:
        start_from = now_local()

    results: List[Tuple[str, str]] = []
    # round to next step
    minutes = (start_from.minute // SLOT_STEP_MIN + 1) * SLOT_STEP_MIN
    rounded = start_from.replace(second=0, microsecond=0)
    if minutes >= 60:
        rounded = rounded.replace(minute=0) + timedelta(hours=1)
    else:
        rounded = rounded.replace(minute=minutes)

    for day_offset in range(days_ahead + 1):
        d = (rounded.date() + timedelta(days=day_offset))
        day_start = datetime(d.year, d.month, d.day, WORK_START_HOUR, 0, tzinfo=TZ)
        day_end = datetime(d.year, d.month, d.day, WORK_END_HOUR, 0, tzinfo=TZ)

        cur = max(day_start, rounded if day_offset == 0 else day_start)
        while cur < day_end:
            date_str = cur.strftime("%Y-%m-%d")
            time_str = cur.strftime("%H:%M")
            if is_future_slot(date_str, time_str):
                end_dt = cur + timedelta(minutes=duration_min)
                try:
                    if is_time_available(cur, end_dt):
                        results.append((date_str, time_str))
                        if len(results) >= limit:
                            return results
                except Exception as e:
                    # If calendar check fails, don't crash the bot; just stop suggesting.
                    logger.exception("Availability check failed: %s", e)
                    return results
            cur += timedelta(minutes=SLOT_STEP_MIN)

    return results

def admin_keyboard(req_id: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=f"confirm:{req_id}")
    kb.button(text="❌ Отменить", callback_data=f"cancel:{req_id}")
    kb.adjust(2)
    return kb.as_markup()

async def notify_admins(bot: Bot, text: str, reply_markup=None):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=reply_markup)
        except Exception as e:
            logger.warning("Cannot notify admin %s: %s", admin_id, e)

# -----------------------------
# Bot setup
# -----------------------------
bot = Bot(BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

# -----------------------------
# Commands /start /book
# -----------------------------
@dp.message(CommandStart())
async def start_cmd(message: Message):
    await message.answer(
        "Привет! Я администратор записи.\n\n"
        "Чтобы записаться — нажмите /book или напишите, например:\n"
        "• «сегодня 18:30 массаж»\n"
        "• «завтра 11:00 тайский массаж, имя Раис, телефон +7...»"
    )

@dp.message(Command("book"))
async def book_cmd(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(BookingFSM.service)
    await message.answer("Какую услугу хотите? (например: Тайский массаж / Oil / Foot)")

# -----------------------------
# FSM steps
# -----------------------------
@dp.message(BookingFSM.service)
async def fsm_service(message: Message, state: FSMContext):
    service_name = (message.text or "").strip()
    if len(service_name) < 2:
        await message.answer("Введите название услуги текстом.")
        return
    await state.update_data(service_name=service_name)

    await state.set_state(BookingFSM.date)
    await message.answer("Введите дату (YYYY-MM-DD или DD.MM.YYYY) или скажите «сегодня/завтра».")

@dp.message(BookingFSM.date)
async def fsm_date(message: Message, state: FSMContext):
    date_str, _ = parse_date_time_from_text(message.text or "")
    if not date_str:
        await message.answer("Не понял дату. Пример: 2026-01-18 или 18.01.2026 или «завтра».")
        return
    await state.update_data(date_str=date_str)

    await state.set_state(BookingFSM.time)
    await message.answer("Введите время (HH:MM), например 18:30")

@dp.message(BookingFSM.time)
async def fsm_time(message: Message, state: FSMContext):
    _, time_str = parse_date_time_from_text(message.text or "")
    if not time_str:
        await message.answer("Не понял время. Пример: 18:30")
        return

    data = await state.get_data()
    date_str = data["date_str"]

    # forbid past
    if not is_future_slot(date_str, time_str):
        slots = suggest_next_slots(data.get("service_name", ""), limit=6)
        if slots:
            pretty = "\n".join([f"• {d} {t}" for d, t in slots])
            await message.answer(
                f"Это время уже прошло (или слишком близко).\n"
                f"Ближайшие доступные слоты:\n{pretty}\n\n"
                f"Введите одно из времен (дата и время) или укажите другое время."
            )
        else:
            await message.answer("Это время уже прошло. Укажите другое время на будущее.")
        return

    # availability check
    start_dt = local_dt(date_str, time_str)
    end_dt = start_dt + timedelta(minutes=DEFAULT_DURATION_MIN)
    try:
        free = is_time_available(start_dt, end_dt)
    except Exception as e:
        logger.exception("Calendar availability check error: %s", e)
        await message.answer("Не удалось проверить календарь. Попробуйте другое время чуть позже.")
        return

    if not free:
        slots = suggest_next_slots(data.get("service_name", ""), start_from=start_dt, limit=6)
        if slots:
            pretty = "\n".join([f"• {d} {t}" for d, t in slots])
            await message.answer(
                f"Это время занято.\nБлижайшие доступные слоты:\n{pretty}\n\n"
                f"Введите другое время."
            )
        else:
            await message.answer("Это время занято. Укажите другое время.")
        return

    await state.update_data(time_str=time_str)

    await state.set_state(BookingFSM.name)
    await message.answer("Как вас зовут?")

@dp.message(BookingFSM.name)
async def fsm_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Введите имя текстом.")
        return
    await state.update_data(client_name=name)

    await state.set_state(BookingFSM.phone)
    await message.answer("Телефон для связи? (можно в любом формате)")

@dp.message(BookingFSM.phone)
async def fsm_phone(message: Message, state: FSMContext):
    phone = (message.text or "").strip()
    if len(phone) < 5:
        await message.answer("Похоже на слишком короткий телефон. Введите еще раз.")
        return
    await state.update_data(phone=phone)

    await state.set_state(BookingFSM.comment)
    await message.answer("Комментарий (необязательно). Можно написать «-».")

@dp.message(BookingFSM.comment)
async def fsm_comment(message: Message, state: FSMContext):
    comment = (message.text or "").strip()
    if comment == "-":
        comment = ""
    data = await state.get_data()

    req_id = uuid.uuid4().hex[:10]
    pending = PendingRequest(
        req_id=req_id,
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        created_at=now_local().isoformat(),
        service_name=data["service_name"],
        client_name=data["client_name"],
        phone=data["phone"],
        date_str=data["date_str"],
        time_str=data["time_str"],
        comment=comment,
        duration_min=DEFAULT_DURATION_MIN,
    )
    PENDING[req_id] = pending

    # Log
    logger.info("NEW_PENDING %s", asdict(pending))

    # notify admins
    admin_text = (
        "🆕 <b>Новая заявка на запись</b>\n"
        f"ID: <code>{req_id}</code>\n"
        f"Клиент: <b>{pending.client_name}</b>\n"
        f"Тел: <code>{pending.phone}</code>\n"
        f"Услуга: <b>{pending.service_name}</b>\n"
        f"Когда: <b>{pending.date_str} {pending.time_str}</b>\n"
        f"Комментарий: {pending.comment or '—'}"
    )
    await notify_admins(bot, admin_text, reply_markup=admin_keyboard(req_id))

    await message.answer(
        "Заявка отправлена администратору ✅\n"
        "Я напишу вам, когда администратор подтвердит или отменит запись."
    )
    await state.clear()

# -----------------------------
# Admin callbacks
# -----------------------------
def _require_admin(cb: CallbackQuery) -> bool:
    return cb.from_user and cb.from_user.id in ADMIN_IDS

@dp.callback_query(F.data.startswith("confirm:"))
async def cb_confirm(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("Недостаточно прав.", show_alert=True)
        return

    req_id = cb.data.split(":", 1)[1].strip()
    pending = PENDING.get(req_id)
    if not pending:
        await cb.answer("Заявка не найдена (возможно уже обработана).", show_alert=True)
        return

    # re-check future and availability (safety)
    if not is_future_slot(pending.date_str, pending.time_str):
        await cb.answer("Время уже прошло — нельзя подтвердить.", show_alert=True)
        await bot.send_message(pending.chat_id, "К сожалению, это время уже прошло. Пожалуйста, выберите новую дату/время: /book")
        PENDING.pop(req_id, None)
        return

    start_dt = local_dt(pending.date_str, pending.time_str)
    end_dt = start_dt + timedelta(minutes=pending.duration_min)
    try:
        if not is_time_available(start_dt, end_dt):
            await cb.answer("Слот уже занят.", show_alert=True)
            slots = suggest_next_slots(pending.service_name, start_from=start_dt, limit=6)
            if slots:
                pretty = "\n".join([f"• {d} {t}" for d, t in slots])
                await bot.send_message(pending.chat_id, f"Это время уже занято. Ближайшие слоты:\n{pretty}\n\nНапишите /book чтобы выбрать.")
            else:
                await bot.send_message(pending.chat_id, "Это время уже занято. Напишите /book чтобы выбрать другое.")
            PENDING.pop(req_id, None)
            return
    except Exception as e:
        logger.exception("Availability check failed on confirm: %s", e)
        await cb.answer("Ошибка проверки календаря. Попробуйте позже.", show_alert=True)
        return

    # Create calendar booking
    try:
        link = create_booking(
            pending.client_name,
            pending.phone,
            pending.service_name,
            pending.date_str,
            pending.time_str,
        )
    except Exception as e:
        logger.exception("create_booking failed: %s", e)
        await cb.answer("Ошибка при создании записи.", show_alert=True)
        return

    # Log
    logger.info("CONFIRMED %s link=%s admin=%s", asdict(pending), link, cb.from_user.id)

    # Notify user and admins
    user_text = (
        "✅ <b>Запись подтверждена!</b>\n"
        f"{pending.service_name}\n"
        f"Когда: <b>{pending.date_str} {pending.time_str}</b>\n"
        f"Имя: <b>{pending.client_name}</b>\n"
        f"Телефон: <code>{pending.phone}</code>\n"
    )
    if link:
        user_text += f"\nСсылка на запись: {link}"

    await bot.send_message(pending.chat_id, user_text)

    await cb.message.edit_text(cb.message.html_text + "\n\n✅ <b>Подтверждено</b>")
    await cb.answer("Подтверждено ✅")

    PENDING.pop(req_id, None)

@dp.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("Недостаточно прав.", show_alert=True)
        return

    req_id = cb.data.split(":", 1)[1].strip()
    pending = PENDING.get(req_id)
    if not pending:
        await cb.answer("Заявка не найдена (возможно уже обработана).", show_alert=True)
        return

    # Log
    logger.info("CANCELLED %s admin=%s", asdict(pending), cb.from_user.id)

    await bot.send_message(
        pending.chat_id,
        "❌ Администратор отменил заявку на запись.\n"
        "Напишите /book чтобы выбрать другое время."
    )
    await cb.message.edit_text(cb.message.html_text + "\n\n❌ <b>Отменено</b>")
    await cb.answer("Отменено ❌")

    PENDING.pop(req_id, None)

# -----------------------------
# Free chat -> auto-route to booking
# -----------------------------
@dp.message(F.text)
async def free_chat_router(message: Message, state: FSMContext):
    """
    If user writes something like "сегодня 10:00 массаж", we start /book and prefill.
    Otherwise: fallback hint.
    """
    if message.text.startswith("/"):
        return

    t = message.text.lower()
    looks_like_booking = any(k in t for k in ["запис", "запишите", "хочу", "бронь", "заброн", "массаж"])
    date_str, time_str = parse_date_time_from_text(t)

    if looks_like_booking and (date_str or time_str):
        await state.clear()
        await state.set_state(BookingFSM.service)

        # try to infer service from text (very naive)
        service_guess = None
        for s in ["тайский", "thai", "oil", "foot", "балий", "спорт", "релакс", "массаж"]:
            if s in t:
                service_guess = "Тайский массаж" if s in ["тайский", "thai"] else s.capitalize()
                break
        if service_guess:
            await state.update_data(service_name=service_guess)

        if date_str:
            await state.update_data(date_str=date_str)
        if time_str:
            await state.update_data(time_str=time_str)

        # If we already have service+date+time, jump to name (with validation later)
        data = await state.get_data()
        if data.get("service_name") and data.get("date_str") and data.get("time_str"):
            # Validate quickly and move forward
            if not is_future_slot(data["date_str"], data["time_str"]):
                slots = suggest_next_slots(data["service_name"], limit=6)
                if slots:
                    pretty = "\n".join([f"• {d} {tm}" for d, tm in slots])
                    await message.answer(
                        f"Это время уже прошло.\nБлижайшие слоты:\n{pretty}\n\n"
                        f"Напишите дату и время из списка или нажмите /book."
                    )
                else:
                    await message.answer("Это время уже прошло. Нажмите /book чтобы записаться на будущее.")
                await state.clear()
                return

            start_dt = local_dt(data["date_str"], data["time_str"])
            end_dt = start_dt + timedelta(minutes=DEFAULT_DURATION_MIN)
            try:
                if not is_time_available(start_dt, end_dt):
                    slots = suggest_next_slots(data["service_name"], start_from=start_dt, limit=6)
                    if slots:
                        pretty = "\n".join([f"• {d} {tm}" for d, tm in slots])
                        await message.answer(f"Это время занято.\nБлижайшие слоты:\n{pretty}\n\nНапишите другое время или /book.")
                    else:
                        await message.answer("Это время занято. Нажмите /book чтобы выбрать другое.")
                    await state.clear()
                    return
            except Exception as e:
                logger.exception("Availability check failed in free chat: %s", e)

            await state.set_state(BookingFSM.name)
            await message.answer("Понял. Как вас зовут?")
            return

        # Otherwise continue FSM from the first missing field
        if not data.get("service_name"):
            await message.answer("Какая услуга? (например: Тайский массаж)")
            return
        if not data.get("date_str"):
            await state.set_state(BookingFSM.date)
            await message.answer("На какую дату? (YYYY-MM-DD / DD.MM.YYYY / сегодня / завтра)")
            return
        if not data.get("time_str"):
            await state.set_state(BookingFSM.time)
            await message.answer("На какое время? (HH:MM)")
            return

    # Default fallback
    await message.answer("Чтобы записаться, напишите /book или просто напишите дату и время (например: «завтра 18:30 тайский массаж»).")

# -----------------------------
# Entrypoint
# -----------------------------
async def main():
    logger.info("Start polling")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
