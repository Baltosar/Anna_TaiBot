import asyncio
import os

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

from ai import ai_reply
from booking import create_booking

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
async def book_time(message: Message, state: FSMContext):
    data = await state.get_data()

    name = data["name"]
    phone = data["phone"]
    service = data["service"]
    date = data["date"]
    time = message.text

    link = create_booking(
        name=name,
        phone=phone,
        service_name=service,
        date=date,
        time=time
    )

    if not link:
        await message.answer(
            "❌ Это время уже занято. Пожалуйста, выберите другое."
        )
        return

    await message.answer(
        f"✅ Клиент записан!\n\n"
        f"📅 Дата: {date}\n"
        f"⏰ Время: {time}\n"
        f"🔗 Ссылка на событие:\n{link}"
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
async def handle_message(message: types.Message):
    if message.chat.id in handoff_users:
        await bot.send_message(
            ADMIN_CHAT_ID,
            f"💬 Клиент ({message.chat.id}):\n{message.text}"
        )
        return

    history = user_memory.get(message.chat.id, [])
    history.append({"role": "user", "content": message.text})

    reply = await ai_reply(history)

    history.append({"role": "assistant", "content": reply})
    user_memory[message.chat.id] = history[-10:]

    await message.answer(reply)

# ====== START ======
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
