import asyncio
import os
from booking import create_booking
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from ai import ai_reply
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage


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
storage = MemoryStorage()
dp = Dispatcher(storage=storage)


# ====== STATE ======
user_memory = {}          # история диалога с AI
handoff_users = set()     # клиенты, переданные администратору
admin_active_user = None  # выбранный клиент у администратора

# ====== BOOKING STATES ======
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
async def book_date(message: types.Message, state: FSMContext):
    await state.update_data(date=message.text)
    await message.answer("Время? (ЧЧ:ММ)")
    await state.set_state(BookingState.time)

@dp.message(BookingState.time)
async def book_time(message: types.Message, state: FSMContext):
    data = await state.get_data()

    link = create_booking(
        name=data["name"],
        phone=data["phone"],
        service_name=data["service"],
        date=data["date"],
        time=message.text
    )

    await message.answer(
        "✅ Вы записаны!\n"
        "📅 Запись добавлена в календарь\n"
        f"{link}"
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
        f"📩 Новый клиент\n"
        f"ID: {message.chat.id}\n"
        f"Username: @{message.from_user.username}"
    )

# ====== ADMIN: LIST CLIENTS ======
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

    text += "\n✏️ Напишите ID клиента, чтобы выбрать его"
    await message.answer(text)

# ====== ADMIN: SELECT CLIENT ======
@dp.message(lambda m: m.chat.id == ADMIN_CHAT_ID and m.text.isdigit())
async def select_client(message: types.Message):
    global admin_active_user

    client_id = int(message.text)

    if client_id not in handoff_users:
        await message.answer("❌ Клиент с таким ID не найден")
        return

    admin_active_user = client_id
    await message.answer(
        f"✅ Вы выбрали клиента {client_id}\n"
        f"Теперь все ваши сообщения будут отправляться ему"
    )

# ====== ADMIN: END DIALOG ======
@dp.message(Command("end"))
async def end_dialog(message: types.Message):
    global admin_active_user

    if message.chat.id != ADMIN_CHAT_ID:
        return

    if not admin_active_user:
        await message.answer("❗ Нет активного диалога")
        return

    client_id = admin_active_user

    # убираем клиента у администратора
    handoff_users.discard(client_id)
    admin_active_user = None

    # сообщение клиенту
    await bot.send_message(
        client_id,
        "🙏 Спасибо за обращение!\n"
        "Теперь вам снова отвечает ассистент 🤖"
    )

    # подтверждение админу
    await message.answer(
        f"✅ Диалог с клиентом {client_id} завершён\n"
        f"Клиент передан обратно AI"
    )

# ====== ADMIN → CLIENT ======
@dp.message(lambda m: m.chat.id == ADMIN_CHAT_ID)
async def admin_reply(message: types.Message):
    if not admin_active_user:
        await message.answer("❗ Сначала выберите клиента (/clients)")
        return

    await bot.send_message(
        admin_active_user,
        f"👩‍💼 Администратор:\n{message.text}"
    )

# ====== USER MESSAGES ======
@dp.message()
async def handle_message(message: types.Message):
    user_id = message.chat.id

    # если клиент передан администратору
    if user_id in handoff_users:
        await bot.send_message(
            ADMIN_CHAT_ID,
            f"💬 Клиент ({user_id}):\n{message.text}"
        )
        return

    # AI-диалог
    history = user_memory.get(user_id, [])
    history.append({"role": "user", "content": message.text})

    reply = await ai_reply(history)

    history.append({"role": "assistant", "content": reply})
    user_memory[user_id] = history[-10:]

    await message.answer(reply)

# ====== START BOT ======
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
