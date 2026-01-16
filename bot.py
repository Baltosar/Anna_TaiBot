import asyncio
import os
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandStart
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from ai import ai_reply
from booking import create_booking as _create_booking

def create_booking_compat(*, name: str, phone: str, service_name: str, date: str, time: str):
    """Call booking.create_booking with backward-compatible arguments.
    Supports both old and new booking.py signatures."""
    last_err = None
    attempts = [
        lambda: _create_booking(name=name, phone=phone, service_name=service_name, date=date, time=time),
        lambda: _create_booking(client_name=name, phone=phone, service_name=service_name, date=date, time=time),
        lambda: _create_booking(name, phone, service_name, date, time),
        lambda: _create_booking(date, time, service_name, name, phone),
        lambda: _create_booking(date, time, service_name),
        lambda: _create_booking(date, time),
    ]
    for fn in attempts:
        try:
            return fn()
        except TypeError as e:
            last_err = e
    raise last_err  # type: ignore[misc]


# ====== ADMIN NOTIFY ======
async def notify_admin(bot, booking: dict, user):
    text = (
        "📅 <b>Новая запись</b>\n\n"
        f"👤 Клиент: {user.full_name}\n"
        f"📞 Telegram: @{user.username or 'нет'}\n"
        f"🧖 Услуга: {booking['service']}\n"
        f"📆 Дата: {booking['date']}\n"
        f"⏰ Время: {booking['time']}\n\n"
        f"🆔 ID клиента: {user.id}"
    )

    await bot.send_message(
        ADMIN_CHAT_ID,
        text,
        parse_mode="HTML"
    )


os.environ["AIOMISC_NO_IPV6"] = "1"

# ====== ENV ======
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

if not ADMIN_CHAT_ID:
    raise RuntimeError("ADMIN_CHAT_ID not set")

ADMIN_CHAT_ID = int(ADMIN_CHAT_ID)

# ====== BOT ======
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ====== STATE ======
user_memory = {}
handoff_users = set()
admin_active_user = None

# ====== BOOKING FSM ======
class BookingState(StatesGroup):
    name = State()
    phone = State()
    service = State()
    date = State()
    time = State()

# ====== KEYBOARD ======
admin_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="👩‍💼 Администратор")]],
    resize_keyboard=True
)

# ====== START ======
@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer(
        "🙏 Добро пожаловать в салон тайского массажа.\n"
        "Я помогу подобрать процедуру и записать вас.\n\n"
        "Напишите, что вас интересует 💆‍♀️",
        reply_markup=admin_kb
    )

# ====== BOOKING FLOW ======
@dp.message(Command("book"))
async def book_start(message: types.Message, state: FSMContext):
    await message.answer("Как вас зовут?")
    await state.set_state(BookingState.name)

@dp.message(BookingState.name)
async def book_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("Ваш телефон?")
    await state.set_state(BookingState.phone)

@dp.message(BookingState.phone)
async def book_phone(message: types.Message, state: FSMContext):
    await state.update_data(phone=message.text)
    await message.answer("Какую процедуру вы хотите?")
    await state.set_state(BookingState.service)

@dp.message(BookingState.service)
async def book_service(message: types.Message, state: FSMContext):
    await state.update_data(service=message.text)
    await message.answer("Дата записи? (ГГГГ-ММ-ДД)")
    await state.set_state(BookingState.date)

@dp.message(BookingState.date)
async def book_date(message: Message, state: FSMContext):
    await state.update_data(date=message.text)
    await message.answer("Время записи? (например 14:00)")
    await state.set_state(BookingState.time)

@dp.message(BookingState.time)
async def book_time(message: Message, state: FSMContext):
    data = await state.get_data()

    name = data["name"]
    phone = data["phone"]
    service = data["service"]
    date = data["date"]
    time = message.text

    link = create_booking_compat(
        name=name,
        phone=phone,
        service_name=service,
        date=date,
        time=time
    )

    if not link:
        await message.answer("❌ Это время уже занято. Пожалуйста, выберите другое.")
        return

    await message.answer(
        f"✅ Клиент записан!\n\n"
        f"📅 Дата: {date}\n"
        f"⏰ Время: {time}\n"
        f"🔗 Ссылка на событие:\n{link}"
    )

    # 🔔 УВЕДОМЛЕНИЕ АДМИНИСТРАТОРУ
    await notify_admin(
        bot,
        booking={
            "service": service,
            "date": date,
            "time": time,
        },
        user=message.from_user
    )

    await state.clear()



# ====== CLIENT → ADMIN ======
@dp.message(lambda m: m.text == "👩‍💼 Администратор")
async def admin_button(message: types.Message):
    handoff_users.add(message.chat.id)

    await message.answer(
        "👩‍💼 Я передал диалог администратору.\n"
        "Он скоро вам ответит 🙏"
    )

    await bot.send_message(
        ADMIN_CHAT_ID,
        f"📩 Новый клиент\nID: {message.chat.id}"
    )

# ====== ADMIN COMMANDS ======
@dp.message(Command("clients"))
async def clients_list(message: types.Message):
    if message.chat.id != ADMIN_CHAT_ID:
        return

    if not handoff_users:
        await message.answer("❗ Нет активных клиентов")
        return

    text = "📋 Клиенты:\n\n"
    for uid in handoff_users:
        marker = "👉 " if uid == admin_active_user else ""
        text += f"{marker}{uid}\n"

    await message.answer(text)

@dp.message(Command("end"))
async def end_dialog(message: types.Message):
    global admin_active_user

    if message.chat.id != ADMIN_CHAT_ID:
        return

    if not admin_active_user:
        await message.answer("❗ Нет активного диалога")
        return

    client_id = admin_active_user
    handoff_users.discard(client_id)
    admin_active_user = None

    await bot.send_message(
        client_id,
        "🙏 Спасибо за обращение!\n"
        "Теперь вам снова отвечает ассистент 🤖"
    )

    await message.answer("✅ Диалог завершён")

@dp.message(lambda m: m.chat.id == ADMIN_CHAT_ID)
async def admin_reply(message: types.Message):
    global admin_active_user

    if message.text.isdigit():
        uid = int(message.text)
        if uid in handoff_users:
            admin_active_user = uid
            await message.answer(f"✅ Вы выбрали клиента {uid}")
        else:
            await message.answer("❌ Клиент не найден")
        return

    if not admin_active_user:
        await message.answer("❗ Сначала выберите клиента")
        return

    await bot.send_message(
        admin_active_user,
        f"👩‍💼 Администратор:\n{message.text}"
    )

# ====== AI ======
@dp.message()
async def handle_message(message: types.Message, state: FSMContext):

    # если диалог передан администратору
    if message.chat.id in handoff_users:
        await bot.send_message(
            ADMIN_CHAT_ID,
            f"💬 Клиент ({message.chat.id}):\n{message.text}"
        )
        return

    history = user_memory.get(message.chat.id, [])
    history.append({"role": "user", "content": message.text})

    # ⚠️ ВАЖНО: await ТОЛЬКО ВНУТРИ async-функции
    reply = await ai_reply(history)

    # 🔥 ЕСЛИ AI ПОНЯЛ, ЧТО ЭТО ЗАПИСЬ
    if "INTENT:BOOKING" in reply:
        await message.answer(
        "Отлично 👍 Я помогу вас записать.\n\n"
        "Как вас зовут?"
        )

         # 🔥 ПРАВИЛЬНО: начинаем FSM С НАЧАЛА
        await state.set_state(BookingState.name)
        return


    # 🔹 обычный AI-ответ
    history.append({"role": "assistant", "content": reply})
    user_memory[message.chat.id] = history[-10:]

    await message.answer(reply)


# ====== START ======
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
@router.callback_query(F.data.startswith(ADMIN_TAKE_PREFIX))
async def take_chat_cb(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Недостаточно прав", show_alert=True)
        return
    user_id = int(call.data.split(ADMIN_TAKE_PREFIX, 1)[1])
    LIVE_USER_TO_ADMIN[user_id] = call.from_user.id
    PENDING_LIVE_USERS.discard(user_id)
    await call.answer("Диалог закреплён за вами")
    try:
        await bot.send_message(
            call.from_user.id,
            f"✅ Вы подключились к клиенту <code>{user_id}</code>. "
            "Чтобы ответить — нажмите «Ответить» на сообщении клиента и напишите текст.",
            reply_markup=admin_end_kb(user_id),
        )
    except Exception:
        logger.exception("Failed to message admin on take")
    try:
        await bot.send_message(
            user_id,
            "✅ Живой администратор подключился. Пишите сюда, я передам.",
            reply_markup=main_menu_kb(),
        )
    except Exception:
        logger.exception("Failed to message user on take")

@router.callback_query(F.data.startswith(ADMIN_END_PREFIX))
async def end_chat_cb(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Недостаточно прав", show_alert=True)
        return
    user_id = int(call.data.split(ADMIN_END_PREFIX, 1)[1])
    if LIVE_USER_TO_ADMIN.get(user_id) != call.from_user.id:
        await call.answer("Этот диалог закреплён за другим администратором", show_alert=True)
        return
    LIVE_USER_TO_ADMIN.pop(user_id, None)
    PENDING_LIVE_USERS.discard(user_id)
    await call.answer("Диалог завершён")
    try:
        await bot.send_message(
            user_id,
            "Диалог с администратором завершён. Можете продолжить общение со мной или записаться через /book.",
            reply_markup=main_menu_kb(),
        )
    except Exception:
        logger.exception("Failed to message user on end")
    try:
        await bot.send_message(call.from_user.id, f"Диалог с клиентом <code>{user_id}</code> завершён.")
    except Exception:
        logger.exception("Failed to message admin on end")

@router.message(F.reply_to_message & (F.from_user.id.in_(ADMIN_IDS)))
async def admin_reply_to_user(message: types.Message):
    admin_id = message.from_user.id
    reply = message.reply_to_message
    user_id = ADMIN_REPLY_MAP.get((admin_id, reply.message_id))
    if not user_id:
        return
    try:
        if message.text:
            await bot.send_message(user_id, f"👩‍💼 Администратор: {message.text}")
        else:
            await bot.copy_message(chat_id=user_id, from_chat_id=admin_id, message_id=message.message_id)
    except Exception:
        logger.exception("Failed to send admin reply to user")

@router.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я администратор-бот.\n"
        "Чтобы записаться, нажмите «Записаться» или напишите /book.\n"
        "Чтобы связаться с живым администратором — нажмите «Администратор».",
        reply_markup=main_menu_kb(),
    )

@router.message(F.text == "Записаться")
async def quick_book(message: types.Message, state: FSMContext):
    await cmd_book(message, state)

@router.message(F.text == "Администратор")
async def request_admin(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id in LIVE_USER_TO_ADMIN:
        await message.answer("Администратор уже подключён. Напишите сообщение — я передам.")
        return
    await message.answer("Ок! Сейчас подключу живого администратора. Напишите, пожалуйста, что именно нужно.")
    await notify_admins_live_request(message.from_user, message.chat.id)
