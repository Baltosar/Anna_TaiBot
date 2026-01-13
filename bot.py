# bot.py
import os
import re
import json
import uuid
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties

from booking import check_slot_available, create_booking, suggest_next_slots

# ----------------------------
# Config
# ----------------------------
TZ = ZoneInfo(os.getenv("BOT_TZ", "Europe/Moscow"))
MIN_FUTURE_MINUTES = int(os.getenv("MIN_FUTURE_MINUTES", "5"))  # "сегодня 10:00" запрещать после 10:05
DEFAULT_DURATION_MIN = int(os.getenv("DEFAULT_DURATION_MIN", "60"))

BOT_TOKEN = os.getenv("BOT_TOKEN")

# Support both ADMIN_CHAT_IDS (comma-separated) and legacy ADMIN_CHAT_ID (single)
_admin_ids_raw = os.getenv("ADMIN_CHAT_IDS") or os.getenv("ADMIN_CHAT_ID") or ""
ADMIN_IDS: List[int] = []
if _admin_ids_raw.strip():
    try:
        ADMIN_IDS = [int(x.strip()) for x in _admin_ids_raw.split(",") if x.strip()]
    except ValueError:
        ADMIN_IDS = []

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")
if not ADMIN_IDS:
    raise RuntimeError("ADMIN_CHAT_IDS (or ADMIN_CHAT_ID) not set or invalid")

# ----------------------------
# Logging
# ----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("bot")

# ----------------------------
# FSM
# ----------------------------
class BookingFSM(StatesGroup):
    service = State()
    name = State()
    phone = State()
    date = State()
    time = State()
    comment = State()

# ----------------------------
# Pending store
# ----------------------------
@dataclass
class PendingRequest:
    req_id: str
    user_id: int
    chat_id: int
    created_at: str  # ISO
    service_name: str
    client_name: str
    phone: str
    date_str: str
    time_str: str
    duration_min: int
    comment: str
    status: str = "PENDING"   # PENDING / CONFIRMED / CANCELED
    confirmed_by: Optional[int] = None

PENDING: Dict[str, PendingRequest] = {}

# ----------------------------
# Helpers
# ----------------------------
SERVICE_PRESETS = [
    "Тайский массаж",
    "Массаж спины",
    "Массаж ног",
    "Спа-программа",
]

def _now() -> datetime:
    return datetime.now(TZ)

def _normalize_phone(s: str) -> str:
    s = s.strip()
    s = re.sub(r"[^\d+]", "", s)
    if s.startswith("8") and len(re.sub(r"\D", "", s)) == 11:
        s = "+7" + s[1:]
    return s

def _parse_date_token(token: str) -> Optional[datetime]:
    token = token.strip().lower()
    base = _now().date()

    if token in ("сегодня", "today"):
        return datetime.combine(base, datetime.min.time(), TZ)
    if token in ("завтра", "tomorrow"):
        return datetime.combine(base + timedelta(days=1), datetime.min.time(), TZ)
    if token in ("послезавтра",):
        return datetime.combine(base + timedelta(days=2), datetime.min.time(), TZ)

    # YYYY-MM-DD
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", token)
    if m:
        y, mo, d = map(int, m.groups())
        try:
            return datetime(y, mo, d, tzinfo=TZ)
        except ValueError:
            return None

    # DD.MM.YYYY or DD.MM
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?", token)
    if m:
        d, mo, y = m.groups()
        d = int(d); mo = int(mo); y = int(y) if y else base.year
        try:
            dt = datetime(y, mo, d, tzinfo=TZ)
            # если год не указан и дата уже прошла — переносим на следующий год
            if not m.group(3) and dt.date() < base:
                dt = datetime(y + 1, mo, d, tzinfo=TZ)
            return dt
        except ValueError:
            return None

    # DD-MM-YYYY
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})-(\d{4})", token)
    if m:
        d, mo, y = map(int, m.groups())
        try:
            return datetime(y, mo, d, tzinfo=TZ)
        except ValueError:
            return None

    return None

def _parse_time_token(token: str) -> Optional[Tuple[int, int]]:
    token = token.strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", token)
    if not m:
        return None
    hh, mm = map(int, m.groups())
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return hh, mm
    return None

def extract_datetime(text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (date_str 'YYYY-MM-DD', time_str 'HH:MM') if found in text.
    Accepts:
      - 'сегодня 10:00', 'завтра 18:30'
      - '2026-01-17 17:00'
      - '17.01 17:00' or '17.01.2026 17:00'
      - '17-01-2026 17:00'
      - only time like 'в 10:00' => today
    """
    t = text.strip().lower()

    # date + time (words)
    date_token = None
    time_token = None

    # try word-based date
    for word in ("сегодня", "завтра", "послезавтра"):
        if re.search(rf"\b{word}\b", t):
            date_token = word
            break

    # try numeric date
    if not date_token:
        m = re.search(r"\b(\d{4}-\d{1,2}-\d{1,2})\b", t)
        if m:
            date_token = m.group(1)
    if not date_token:
        m = re.search(r"\b(\d{1,2}\.\d{1,2}(?:\.\d{4})?)\b", t)
        if m:
            date_token = m.group(1)
    if not date_token:
        m = re.search(r"\b(\d{1,2}-\d{1,2}-\d{4})\b", t)
        if m:
            date_token = m.group(1)

    m = re.search(r"\b(\d{1,2}:\d{2})\b", t)
    if m:
        time_token = m.group(1)

    if not time_token and not date_token:
        return None, None

    # If only time — assume today
    if time_token and not date_token:
        date_token = "сегодня"

    dt_date = _parse_date_token(date_token) if date_token else None
    tt = _parse_time_token(time_token) if time_token else None
    if not dt_date or not tt:
        return None, None

    hh, mm = tt
    combined = dt_date.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return combined.date().isoformat(), f"{hh:02d}:{mm:02d}"

def is_future_slot(date_str: str, time_str: str, min_future_minutes: int = MIN_FUTURE_MINUTES) -> bool:
    try:
        y, mo, d = map(int, date_str.split("-"))
        hh, mm = map(int, time_str.split(":"))
        slot = datetime(y, mo, d, hh, mm, tzinfo=TZ)
    except Exception:
        return False
    return slot >= (_now() + timedelta(minutes=min_future_minutes))

def infer_service(text: str) -> Optional[str]:
    t = text.lower()
    for s in SERVICE_PRESETS:
        if s.lower().split()[0] in t:
            return s
    if "тайск" in t:
        return "Тайский массаж"
    if "спин" in t:
        return "Массаж спины"
    if "ног" in t:
        return "Массаж ног"
    return None

def build_admin_keyboard(req_id: str) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=f"adm:confirm:{req_id}")
    kb.button(text="❌ Отменить", callback_data=f"adm:cancel:{req_id}")
    kb.adjust(2)
    return kb

async def notify_admins(bot: Bot, text: str, req_id: Optional[str] = None) -> None:
    for admin_id in ADMIN_IDS:
        try:
            if req_id:
                await bot.send_message(admin_id, text, reply_markup=build_admin_keyboard(req_id).as_markup())
            else:
                await bot.send_message(admin_id, text)
        except Exception as e:
            logger.warning("Failed to notify admin %s: %s", admin_id, e)

# ----------------------------
# Bot/Dispatcher
# ----------------------------
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())

# ----------------------------
# Handlers
# ----------------------------
@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Привет! Я администратор записи.\n\n"
        "Чтобы записаться, напишите <b>/book</b> или просто напишите дату и время, например:\n"
        "• <i>завтра 18:30 тайский массаж</i>\n"
        "• <i>сегодня 10:00</i>"
    )

@dp.message(Command("book"))
async def cmd_book(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(BookingFSM.service)
    await message.answer(
        "Отлично, давайте оформим запись.\n"
        "Какую услугу выбираете?\n"
        + "\n".join([f"• {s}" for s in SERVICE_PRESETS])
    )

@dp.message(BookingFSM.service)
async def step_service(message: Message, state: FSMContext):
    service = message.text.strip()
    if len(service) < 2:
        await message.answer("Напишите название услуги текстом.")
        return
    await state.update_data(service_name=service)
    await state.set_state(BookingFSM.name)
    await message.answer("Как вас зовут?")

@dp.message(BookingFSM.name)
async def step_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if len(name) < 2:
        await message.answer("Напишите имя (минимум 2 символа).")
        return
    await state.update_data(client_name=name)
    await state.set_state(BookingFSM.phone)
    await message.answer("Ваш телефон (можно в формате +7...)?")

@dp.message(BookingFSM.phone)
async def step_phone(message: Message, state: FSMContext):
    phone = _normalize_phone(message.text)
    if len(re.sub(r"\D", "", phone)) < 10:
        await message.answer("Похоже, телефон некорректный. Напишите ещё раз.")
        return
    await state.update_data(phone=phone)
    await state.set_state(BookingFSM.date)
    await message.answer("На какую дату? (например 17.01 или 2026-01-17, или 'завтра')")

@dp.message(BookingFSM.date)
async def step_date(message: Message, state: FSMContext):
    token = message.text.strip().lower()
    dt = _parse_date_token(token)
    if not dt:
        await message.answer("Не понял дату. Пример: 17.01 или 2026-01-17 или 'завтра'.")
        return
    await state.update_data(date_str=dt.date().isoformat())
    await state.set_state(BookingFSM.time)
    await message.answer("Во сколько? (например 18:30)")

@dp.message(BookingFSM.time)
async def step_time(message: Message, state: FSMContext):
    tt = _parse_time_token(message.text.strip())
    if not tt:
        await message.answer("Не понял время. Пример: 18:30")
        return
    hh, mm = tt
    time_str = f"{hh:02d}:{mm:02d}"
    data = await state.get_data()
    date_str = data.get("date_str")
    if not date_str:
        await message.answer("Что-то пошло не так с датой. Начните заново: /book")
        await state.clear()
        return

    if not is_future_slot(date_str, time_str):
        # предлагать ближайшие слоты
        slots = suggest_next_slots(duration_minutes=DEFAULT_DURATION_MIN, limit=5)
        if slots:
            pretty = "\n".join([f"• {d} {t}" for d, t in slots])
            await message.answer(
                "Это время уже в прошлом или слишком близко к текущему.\n"
                "Ближайшие свободные варианты:\n" + pretty + "\n\n"
                "Напишите один из вариантов в формате <b>YYYY-MM-DD HH:MM</b> или другой."
            )
            return
        await message.answer("Это время уже недоступно. Попробуйте другое время.")
        return

    # check calendar availability
    if not check_slot_available(date_str=date_str, time_str=time_str, duration_minutes=DEFAULT_DURATION_MIN):
        slots = suggest_next_slots(duration_minutes=DEFAULT_DURATION_MIN, limit=5)
        if slots:
            pretty = "\n".join([f"• {d} {t}" for d, t in slots])
            await message.answer(
                "К сожалению, это окно занято.\n"
                "Ближайшие свободные варианты:\n" + pretty
            )
            return
        await message.answer("К сожалению, это окно занято. Попробуйте другое время.")
        return

    await state.update_data(time_str=time_str)
    await state.set_state(BookingFSM.comment)
    await message.answer("Комментарий/пожелания? (если нет —suggestion: напишите «нет»)")

@dp.message(BookingFSM.comment)
async def step_comment(message: Message, state: FSMContext):
    comment = message.text.strip()
    if comment.lower() in ("нет", "не нужно", "-", "no"):
        comment = ""
    data = await state.get_data()

    req_id = uuid.uuid4().hex[:10]
    req = PendingRequest(
        req_id=req_id,
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        created_at=_now().isoformat(),
        service_name=data["service_name"],
        client_name=data["client_name"],
        phone=data["phone"],
        date_str=data["date_str"],
        time_str=data["time_str"],
        duration_min=DEFAULT_DURATION_MIN,
        comment=comment,
    )
    PENDING[req_id] = req
    logger.info("NEW_PENDING %s", asdict(req))

    text = (
        f"📌 <b>Новая заявка</b> #{req_id}\n"
        f"👤 {req.client_name} ({req.phone})\n"
        f"🧖 {req.service_name} / {req.duration_min} мин\n"
        f"🗓 {req.date_str} {req.time_str} (МСК)\n"
        + (f"💬 {req.comment}\n" if req.comment else "")
        + f"\nПодтвердить или отменить?"
    )
    await notify_admins(bot, text, req_id=req_id)
    await message.answer("Заявка отправлена администратору ✅\nЯ напишу вам, когда её подтвердят.")
    await state.clear()

# ----------------------------
# Admin callbacks
# ----------------------------
@dp.callback_query(F.data.startswith("adm:confirm:"))
async def admin_confirm(cb: CallbackQuery):
    admin_id = cb.from_user.id
    req_id = cb.data.split(":")[-1]
    req = PENDING.get(req_id)

    if not req:
        await cb.answer("Заявка не найдена", show_alert=True)
        return
    if req.status != "PENDING":
        await cb.answer(f"Уже обработано: {req.status}", show_alert=True)
        return

    # Re-check time validity and availability
    if not is_future_slot(req.date_str, req.time_str):
        req.status = "CANCELED"
        req.confirmed_by = admin_id
        await cb.answer("Время уже в прошлом — отменено", show_alert=True)
        try:
            await bot.send_message(req.chat_id, "К сожалению, выбранное время уже прошло. Попробуйте выбрать другое.")
        except Exception:
            pass
        return

    if not check_slot_available(req.date_str, req.time_str, req.duration_min):
        await cb.answer("Слот уже занят", show_alert=True)
        try:
            slots = suggest_next_slots(duration_minutes=req.duration_min, limit=5)
            if slots:
                pretty = "\n".join([f"• {d} {t}" for d, t in slots])
                await bot.send_message(req.chat_id, "К сожалению, это время уже занято.\nБлижайшие варианты:\n" + pretty)
        except Exception:
            pass
        return

    link = create_booking(
        date_str=req.date_str,
        time_str=req.time_str,
        service_name=req.service_name,
        client_name=req.client_name,
        phone=req.phone,
        duration_minutes=req.duration_min,
        comment=req.comment,
    )
    req.status = "CONFIRMED"
    req.confirmed_by = admin_id
    logger.info("CONFIRMED %s link=%s admin=%s", asdict(req), link, admin_id)

    # Notify client
    msg_client = (
        "✅ <b>Запись подтверждена!</b>\n"
        f"🗓 {req.date_str} {req.time_str} (МСК)\n"
        f"🧖 {req.service_name} / {req.duration_min} мин\n"
        + (f"\nСсылка: {link}" if link else "")
    )
    await bot.send_message(req.chat_id, msg_client)

    # Notify all admins (so everyone sees result)
    await notify_admins(bot, f"✅ Заявка #{req_id} подтверждена админом <code>{admin_id}</code>.\n{msg_client}")

    await cb.answer("Подтверждено ✅")
    # optionally edit message
    try:
        await cb.message.edit_text(cb.message.text + f"\n\n✅ Подтверждено админом {admin_id}", reply_markup=None)
    except Exception:
        pass

@dp.callback_query(F.data.startswith("adm:cancel:"))
async def admin_cancel(cb: CallbackQuery):
    admin_id = cb.from_user.id
    req_id = cb.data.split(":")[-1]
    req = PENDING.get(req_id)

    if not req:
        await cb.answer("Заявка не найдена", show_alert=True)
        return
    if req.status != "PENDING":
        await cb.answer(f"Уже обработано: {req.status}", show_alert=True)
        return

    req.status = "CANCELED"
    req.confirmed_by = admin_id
    logger.info("CANCELED %s admin=%s", asdict(req), admin_id)

    await bot.send_message(req.chat_id, "❌ Запись отменена администратором. Можете выбрать другое время.")
    await notify_admins(bot, f"❌ Заявка #{req_id} отменена админом <code>{admin_id}</code>.", req_id=None)

    await cb.answer("Отменено ❌")
    try:
        await cb.message.edit_text(cb.message.text + f"\n\n❌ Отменено админом {admin_id}", reply_markup=None)
    except Exception:
        pass

# ----------------------------
# AI chat handler
# ----------------------------
def is_booking_intent(text: str) -> bool:
    t = text.lower()
    if "/book" in t:
        return True
    # any time mention or date keyword
    if re.search(r"\b(\d{1,2}:\d{2})\b", t):
        return True
    if any(w in t for w in ["сегодня", "завтра", "послезавтра"]):
        return True
    if re.search(r"\b\d{4}-\d{1,2}-\d{1,2}\b", t) or re.search(r"\b\d{1,2}\.\d{1,2}\b", t):
        return True
    if "запис" in t or "брон" in t:
        return True
    return False

@dp.message()
async def handle_message(message: Message, state: FSMContext):
    # If FSM active, ignore (aiogram routes to state handlers)
    if await state.get_state():
        return

    text = (message.text or "").strip()
    if not text:
        return

    # If user explicitly asks /book in free chat
    if text.startswith("/book"):
        return await cmd_book(message, state)

    # Booking intent: try extract date/time
    if is_booking_intent(text):
        date_str, time_str = extract_datetime(text)
        service = infer_service(text)

        if date_str and time_str:
            # Validate future
            if not is_future_slot(date_str, time_str):
                slots = suggest_next_slots(duration_minutes=DEFAULT_DURATION_MIN, limit=5)
                if slots:
                    pretty = "\n".join([f"• {d} {t}" for d, t in slots])
                    await message.answer(
                        "Это время уже прошло или слишком близко к текущему.\n"
                        "Ближайшие свободные варианты:\n" + pretty + "\n\n"
                        "Напишите подходящий вариант."
                    )
                    return
                await message.answer("Это время уже недоступно. Напишите другое время.")
                return

            # Ask missing fields via FSM
            await state.clear()
            await state.update_data(date_str=date_str, time_str=time_str)
            await state.set_state(BookingFSM.service)
            if service:
                await state.update_data(service_name=service)
                await state.set_state(BookingFSM.name)
                await message.answer(f"Ок, записываем на <b>{date_str} {time_str}</b>.\nКак вас зовут?")
            else:
                await message.answer(
                    f"Ок, записываем на <b>{date_str} {time_str}</b>.\n"
                    "Какая услуга?"
                )
            return

        # Not enough info: gently ask
        await message.answer(
            "Чтобы записаться, напишите дату и время, например:\n"
            "• <i>завтра 18:30 тайский массаж</i>\n"
            "• <i>2026-01-17 17:00</i>\n"
            "Или подтверждите через <b>/book</b>."
        )
        return

    # Otherwise: normal AI response
    try:
        from ai import ai_reply  # your ai.py should expose ai_reply(history)->str
        history = [{"role": "user", "content": text}]
        reply = await ai_reply(history)
        await message.answer(reply)
    except Exception as e:
        logger.exception("AI reply failed: %s", e)
        await message.answer("Извините, произошла ошибка. Попробуйте ещё раз или используйте /book.")

# ----------------------------
# Entrypoint
# ----------------------------
async def main():
    logger.info("Start polling")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
