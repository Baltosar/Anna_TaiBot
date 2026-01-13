import asyncio
import os
import uuid
import logging
import re
from datetime import datetime

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from ai import ai_reply
from booking import (
    create_booking,
    check_slot_available,
    suggest_next_free_slots,
    parse_datetime_from_text,
)

os.environ["AIOMISC_NO_IPV6"] = "1"

# ====== LOGGING ======
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("bot")

BOOKINGS_LOG_PATH = "bookings.log"


def log_booking_line(text: str) -> None:
    try:
        with open(BOOKINGS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(text.rstrip() + "\n")
    except Exception as e:
        logger.exception(f"Failed to write bookings log: {e}")


# ====== ENV ======
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID_RAW = os.getenv("ADMIN_CHAT_ID")  # "id1,id2"
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

if not ADMIN_CHAT_ID_RAW:
    raise RuntimeError("ADMIN_CHAT_ID not set")

ADMIN_IDS = [int(x.strip()) for x in ADMIN_CHAT_ID_RAW.split(",") if x.strip().isdigit()]
if not ADMIN_IDS:
    raise RuntimeError("ADMIN_CHAT_ID has no valid IDs")

# ====== BOT ======
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ====== MEMORY & ADMIN STATE ======
user_memory = {}  # user_id -> history list
handoff_users = set()  # users currently in admin mode
admin_active_user = {}  # admin_id -> selected client_id
admin_clients = {}  # client_id -> {"username": "...", "first_name": "..."}
pending_bookings = {}  # booking_req_id -> dict(data)

# ====== KEYBOARD ======
admin_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="👩‍💼 Администратор")]],
    resize_keyboard=True
)

# ====== FSM ======
class BookingStates(StatesGroup):
    name = State()
    phone = State()
    service = State()
    date = State()
    time = State()


# ====== HELPERS ======
async def notify_admins(text: str, reply_markup=None):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=reply_markup)
        except Exception as e:
            logger.warning(f"Cannot send to admin {admin_id}: {e}")


def booking_admin_keyboard(req_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"bk:ok:{req_id}"),
        InlineKeyboardButton(text="❌ Отменить", callback_data=f"bk:no:{req_id}")
    ]])


def safe_username(u: types.User) -> str:
    if u.username:
        return f"@{u.username}"
    return f"{u.first_name or ''}".strip() or "без username"


def format_slots(slots: list[tuple[str, str]]) -> str:
    if not slots:
        return "К сожалению, ближайших слотов не нашёл 😕"
    lines = []
    for d, t in slots:
        lines.append(f"• {d} {t} (МСК)")
    return "\n".join(lines)


async def create_pending_request_from_state(message: types.Message, state: FSMContext, date_str: str, time_str: str):
    """
    Общая финализация: проверяем слот + создаём заявку на админ-подтверждение.
    """
    data = await state.get_data()
    name = data.get("name", "")
    phone = data.get("phone", "")
    service_name = data.get("service", "")

    # 1) Проверка слота (учитывает "в будущем" внутри booking.py)
    free = check_slot_available(date_str=date_str, time_str=time_str, duration_minutes=60)
    if not free:
        # предлагаем ближайшие
        # старт от указанного времени, чтобы “рядом” предлагать
        try:
            start_dt_pref = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        except Exception:
            start_dt_pref = None

        slots = suggest_next_free_slots(limit=5)
        await message.answer(
            "⛔ Это время недоступно (занято или уже прошло).\n"
            "Вот ближайшие свободные варианты:\n"
            f"{format_slots(slots)}\n\n"
            "Напишите один из вариантов (например: “завтра 18:30” или “2026-01-15 10:00”)."
        )
        await state.clear()
        return

    # 2) Создаём заявку на подтверждение админом
    req_id = uuid.uuid4().hex[:10]
    pending_bookings[req_id] = {
        "user_id": message.chat.id,
        "name": name,
        "phone": phone,
        "service": service_name,
        "date": date_str,
        "time": time_str,
        "duration": 60,
    }

    await message.answer(
        "✅ Заявка на запись создана!\n"
        "Я отправил её администратору на подтверждение 🙏\n"
        "Как только подтвердят — пришлю вам итог."
    )

    admin_text = (
        "🆕 Заявка на запись\n"
        f"ID заявки: {req_id}\n"
        f"Клиент ID: {message.chat.id}\n"
        f"Имя: {name}\n"
        f"Телефон: {phone}\n"
        f"Услуга: {service_name}\n"
        f"Дата/время: {date_str} {time_str} (МСК)\n\n"
        "Подтвердить запись?"
    )

    log_booking_line(
        f"[REQUEST] req_id={req_id} user_id={message.chat.id} "
        f"name={name} phone={phone} service={service_name} "
        f"datetime={date_str} {time_str} MSK"
    )

    await notify_admins(admin_text, reply_markup=booking_admin_keyboard(req_id))
    await state.clear()


def looks_like_booking_intent(text: str) -> bool:
    t = (text or "").lower()
    keywords = ["запиши", "запис", "бронь", "заброни", "хочу", "массаж", "сеанс"]
    return any(k in t for k in keywords)


def extract_service_hint(text: str) -> str | None:
    """
    Очень простой эвристический “намёк” на услугу.
    Если не нашли — вернём None, тогда FSM спросит.
    """
    t = (text or "").lower()
    if "тайск" in t:
        return "Тайский массаж"
    if "мас" in t:
        return "Массаж"
    return None


# ====== COMMANDS ======
@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer(
        "🙏 Добро пожаловать в салон тайского массажа.\n"
        "Я помогу подобрать процедуру и записать вас.\n\n"
        "Напишите, что вас интересует 💆‍♀️",
        reply_markup=admin_kb
    )


@dp.message(F.text == "👩‍💼 Администратор")
async def admin_button(message: types.Message):
    user_id = message.chat.id
    handoff_users.add(user_id)

    admin_clients[user_id] = {
        "username": message.from_user.username or "",
        "first_name": message.from_user.first_name or ""
    }

    await message.answer(
        "👩‍💼 Я передал диалог администратору.\n"
        "Он скоро вам ответит 🙏"
    )

    await notify_admins(
        "📩 Новый клиент (перевод к администратору)\n"
        f"ID: {user_id}\n"
        f"Username: {safe_username(message.from_user)}\n"
        "Команда админа: /clients → выбери ID клиента"
    )


@dp.message(Command("clients"))
async def clients_list(message: types.Message):
    if message.chat.id not in ADMIN_IDS:
        return

    if not handoff_users:
        await message.answer("❗ Нет активных клиентов")
        return

    text = "📋 Клиенты в админ-режиме:\n\n"
    for uid in sorted(handoff_users):
        marker = "👉 " if admin_active_user.get(message.chat.id) == uid else ""
        info = admin_clients.get(uid, {})
        uname = info.get("username", "")
        first = info.get("first_name", "")
        label = f"{first}".strip() or ""
        if uname:
            label = (label + " " + f"@{uname}").strip()
        if label:
            text += f"{marker}ID: {uid} ({label})\n"
        else:
            text += f"{marker}ID: {uid}\n"

    text += "\n✏️ Напиши ID клиента, чтобы выбрать его (после этого твои сообщения пойдут ему)."
    await message.answer(text)


@dp.message(F.text.regexp(r"^\d+$"))
async def admin_select_client(message: types.Message):
    if message.chat.id not in ADMIN_IDS:
        return

    uid = int(message.text)
    if uid not in handoff_users:
        await message.answer("❌ Клиент с таким ID не найден (или уже вышел из админ-режима).")
        return

    admin_active_user[message.chat.id] = uid
    await message.answer(f"✅ Вы выбрали клиента ID {uid}\nТеперь все ваши сообщения будут отправляться ему.")


@dp.message(Command("end"))
async def end_dialog(message: types.Message, state: FSMContext):
    if message.chat.id in ADMIN_IDS:
        uid = admin_active_user.get(message.chat.id)
        if not uid:
            await message.answer("❗ Сначала выбери клиента через /clients")
            return

        if uid in handoff_users:
            handoff_users.remove(uid)

        admin_active_user[message.chat.id] = None

        try:
            await bot.send_message(uid, "✅ Диалог с администратором завершён. Возвращаю вас к AI-помощнику 🙏")
        except Exception:
            pass

        await message.answer("✅ Клиент возвращён к AI.")
        return

    user_id = message.chat.id
    if user_id in handoff_users:
        handoff_users.remove(user_id)

    for aid, active_uid in list(admin_active_user.items()):
        if active_uid == user_id:
            admin_active_user[aid] = None

    await state.clear()
    await message.answer("✅ Возвращаю вас к AI-помощнику. Чем помочь? 🙏")


# ====== BOOKING FLOW ======
@dp.message(Command("book"))
async def book_start(message: types.Message, state: FSMContext):
    await state.set_state(BookingStates.name)
    await message.answer("📝 Давайте запишем вас.\nКак вас зовут?")


@dp.message(BookingStates.name)
async def book_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(BookingStates.phone)
    await message.answer("📞 Ваш номер телефона?")


@dp.message(BookingStates.phone)
async def book_phone(message: types.Message, state: FSMContext):
    await state.update_data(phone=message.text.strip())
    data = await state.get_data()

    # если услуга уже предзаполнена (из AI-чата) — идём дальше
    if data.get("service"):
        if data.get("date") and data.get("time"):
            await create_pending_request_from_state(message, state, data["date"], data["time"])
            return
        if data.get("date"):
            await state.set_state(BookingStates.time)
            await message.answer("⏰ Время в формате HH:MM (например: 18:30)")
            return

        await state.set_state(BookingStates.date)
        await message.answer("📅 Дата: можно 'сегодня', 'завтра' или YYYY-MM-DD (например: 2026-01-15)")
        return

    await state.set_state(BookingStates.service)
    await message.answer("💆‍♀️ На какую услугу записать? (например: Тайский массаж 60 мин)")


@dp.message(BookingStates.service)
async def book_service(message: types.Message, state: FSMContext):
    await state.update_data(service=message.text.strip())
    data = await state.get_data()

    if data.get("date") and data.get("time"):
        await create_pending_request_from_state(message, state, data["date"], data["time"])
        return

    if data.get("date"):
        await state.set_state(BookingStates.time)
        await message.answer("⏰ Время в формате HH:MM (например: 18:30)")
        return

    await state.set_state(BookingStates.date)
    await message.answer("📅 Дата: можно 'сегодня', 'завтра' или YYYY-MM-DD (например: 2026-01-15)")


@dp.message(BookingStates.date)
async def book_date(message: types.Message, state: FSMContext):
    raw = message.text.strip()
    date_str, time_str = parse_datetime_from_text(raw)

    # если пользователь прислал сразу "сегодня 10:00" на шаге даты — ок
    if date_str and time_str:
        await state.update_data(date=date_str, time=time_str)
        await create_pending_request_from_state(message, state, date_str, time_str)
        return

    # иначе ожидаем чистую дату
    try:
        datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        await message.answer(
            "❗ Неверный формат даты.\n"
            "Можно так:\n"
            "• 2026-01-15\n"
            "• сегодня 18:30\n"
            "• завтра 10:00\n"
            "• 05.01 12:00"
        )
        return

    await state.update_data(date=raw)
    await state.set_state(BookingStates.time)
    await message.answer("⏰ Время в формате HH:MM (например: 18:30)")


@dp.message(BookingStates.time)
async def book_time(message: types.Message, state: FSMContext):
    raw = message.text.strip()
    date_str, time_str = parse_datetime_from_text(raw)

    if date_str and time_str:
        await state.update_data(date=date_str, time=time_str)
        await create_pending_request_from_state(message, state, date_str, time_str)
        return

    # чистое время
    try:
        datetime.strptime(raw, "%H:%M")
    except ValueError:
        await message.answer(
            "❗ Неверный формат времени.\n"
            "Можно так: 18:30\n"
            "Или сразу: сегодня 18:30 / завтра 10:00"
        )
        return

    data = await state.get_data()
    date_str = data.get("date", "")
    if not date_str:
        await message.answer("❗ Сначала укажите дату.")
        await state.set_state(BookingStates.date)
        return

    await state.update_data(time=raw)
    await create_pending_request_from_state(message, state, date_str, raw)


# ====== ADMIN CONFIRMATION CALLBACK ======
@dp.callback_query(F.data.startswith("bk:"))
async def booking_admin_decision(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    action = parts[1]  # ok / no
    req_id = parts[2]

    req = pending_bookings.get(req_id)
    if not req:
        await callback.answer("Заявка уже обработана или не найдена", show_alert=True)
        return

    user_id = req["user_id"]

    if action == "no":
        pending_bookings.pop(req_id, None)
        await callback.message.edit_text(f"❌ Заявка {req_id} отменена администратором.")
        await callback.answer("Отменено")

        log_booking_line(f"[CANCEL] req_id={req_id} admin_id={callback.from_user.id}")

        try:
            await bot.send_message(
                user_id,
                "❌ Администратор отменил запись.\n"
                "Напишите другое время/дату или задайте вопрос — я помогу 🙏"
            )
        except Exception:
            pass
        return

    # action == "ok" → создаём событие
    # перед созданием ещё раз проверим слот (на всякий)
    free = check_slot_available(req["date"], req["time"], duration_minutes=req.get("duration", 60))
    if not free:
        pending_bookings.pop(req_id, None)
        await callback.message.edit_text(
            f"⛔ Заявка {req_id}: время недоступно (занято или уже прошло)."
        )
        await callback.answer("Время недоступно")

        log_booking_line(f"[FAIL_BUSY_OR_PAST] req_id={req_id} admin_id={callback.from_user.id}")

        # предложим альтернативы клиенту
        slots = suggest_next_free_slots(limit=5)
        try:
            await bot.send_message(
                user_id,
                "⛔ Увы, этот слот уже недоступен.\n"
                "Ближайшие свободные варианты:\n"
                f"{format_slots(slots)}\n\n"
                "Напишите один из вариантов, и я отправлю администратору на подтверждение 🙏"
            )
        except Exception:
            pass
        return

    link = create_booking(
        name=req["name"],
        phone=req["phone"],
        service_name=req["service"],
        date_str=req["date"],
        time_str=req["time"],
        duration_minutes=req.get("duration", 60),
    )

    pending_bookings.pop(req_id, None)

    if not link:
        await callback.message.edit_text(f"⛔ Заявка {req_id}: не удалось создать событие (ошибка).")
        await callback.answer("Ошибка")

        log_booking_line(f"[FAIL_CREATE] req_id={req_id} admin_id={callback.from_user.id}")

        try:
            await bot.send_message(
                user_id,
                "⛔ Не получилось создать запись в календаре.\n"
                "Администратор свяжется с вами 🙏"
            )
        except Exception:
            pass
        return

    await callback.message.edit_text(
        f"✅ Заявка {req_id} подтверждена.\n"
        f"Событие создано: {link}"
    )
    await callback.answer("Подтверждено")

    log_booking_line(f"[CONFIRM] req_id={req_id} admin_id={callback.from_user.id} link={link}")

    try:
        await bot.send_message(
            user_id,
            "✅ Запись подтверждена!\n"
            f"📅 {req['date']} {req['time']} (МСК)\n"
            f"💆 {req['service']}\n\n"
            f"Ссылка на событие: {link}"
        )
    except Exception:
        pass


# ====== ADMIN CHAT RELAY ======
@dp.message(F.chat.id.in_(ADMIN_IDS))
async def admin_messages(message: types.Message):
    if message.text and message.text.startswith("/"):
        return

    target = admin_active_user.get(message.chat.id)
    if not target:
        await message.answer("❗ Нет активного клиента. Используй /clients и выбери ID.")
        return

    try:
        await bot.send_message(target, f"👩‍💼 Администратор:\n{message.text}")
    except Exception as e:
        await message.answer(f"❗ Не удалось отправить клиенту: {e}")


# ====== USER MESSAGES ======
@dp.message()
async def handle_message(message: types.Message, state: FSMContext):
    user_id = message.chat.id
    text = message.text or ""

    # если клиент в админ-режиме — пересылаем админам
    if user_id in handoff_users:
        await notify_admins(f"💬 Клиент (ID {user_id}):\n{text}")
        return

    # ====== AI-CHAT → TRY BOOKING ROUTE ======
    # Если человек написал "сегодня 10:00" / "завтра 18:30" и похоже на запись —
    # запускаем FSM и предзаполняем дату/время (и по возможности услугу).
    date_str, time_str = parse_datetime_from_text(text)

    if time_str and looks_like_booking_intent(text):
        # если нет даты — уточним (предложим ближайшие)
        if not date_str:
            slots = suggest_next_free_slots(limit=5)
            await message.answer(
                "Понял, хотите записаться 🙏\n"
                "Напишите дату и время, например: “сегодня 18:30” или “2026-01-15 10:00”.\n\n"
                "Ближайшие свободные варианты:\n"
                f"{format_slots(slots)}"
            )
            return

        # если слот недоступен — предложим ближайшие
        if not check_slot_available(date_str, time_str, duration_minutes=60):
            slots = suggest_next_free_slots(limit=5)
            await message.answer(
                "⛔ Это время недоступно (занято или уже прошло).\n"
                "Ближайшие свободные варианты:\n"
                f"{format_slots(slots)}\n\n"
                "Напишите один из вариантов (например: “завтра 18:30”)."
            )
            return

        # слот ок → переходим в FSM и дальше собираем имя/телефон/услугу
        await state.clear()
        await state.update_data(date=date_str, time=time_str)

        svc = extract_service_hint(text)
        if svc:
            await state.update_data(service=svc)

        await state.set_state(BookingStates.name)
        await message.answer(
            f"Отлично! Записываю на {date_str} {time_str} (МСК).\n"
            "Как вас зовут?"
        )
        return

    # ====== NORMAL AI MODE ======
    history = user_memory.get(user_id, [])
    history.append({"role": "user", "content": text})

    reply = await ai_reply(history)

    history.append({"role": "assistant", "content": reply})
    user_memory[user_id] = history[-10:]

    await message.answer(reply)


# ====== START ======
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

