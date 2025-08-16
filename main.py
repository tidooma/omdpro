# main.py
import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

# ---------- .env загрузка ----------
from dotenv import load_dotenv, dotenv_values

def load_env() -> dict:
    base_dir = Path(__file__).resolve().parent
    # подгружаем все .env* (не переопределяя уже существующие переменные окружения)
    for p in [base_dir, *base_dir.parents]:
        for name in (".env.local", ".env"):
            f = p / name
            if f.exists():
                load_dotenv(dotenv_path=f, override=False)

    env = {}
    # Явно читаем для приоритета: .env.local > .env
    env_local = {}
    env_main = {}
    for p in [base_dir, *base_dir.parents]:
        lf = p / ".env.local"
        if lf.exists():
            env_local = dotenv_values(lf) or {}
            break
    for p in [base_dir, *base_dir.parents]:
        mf = p / ".env"
        if mf.exists():
            env_main = dotenv_values(mf) or {}
            break

    # итоговые значения: OS env > .env.local > .env
    def pick(key: str, default: Optional[str] = None) -> Optional[str]:
        return os.getenv(key) or env_local.get(key) or env_main.get(key) or default

    env["BOT_TOKEN"] = pick("BOT_TOKEN")
    env["CHANNEL_ID"] = pick("CHANNEL_ID", "-1002767095036")  # твой канал
    env["CHANNEL_USERNAME"] = pick("CHANNEL_USERNAME", "@thedozell")  # тег канала
    return env

ENV = load_env()
BOT_TOKEN = ENV["BOT_TOKEN"]
CHANNEL_ID_RAW = ENV["CHANNEL_ID"]
CHANNEL_USERNAME = ENV["CHANNEL_USERNAME"]

# ---------- Константы ----------
ADMIN_ID = 671179325  # твой ID
PRIVATE_CHAT_LINK = "https://t.me/+BsK2e556HeBlNDky"
DB_PATH = "bot.db"

# ---------- Aiogram ----------
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.client.default import DefaultBotProperties

# ---------- SQLAlchemy (async) ----------
from sqlalchemy import String, BigInteger, select
from sqlalchemy.ext.asyncio import (
    create_async_engine, AsyncSession, async_sessionmaker
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

ASYNC_DB_URL = f"sqlite+aiosqlite:///{DB_PATH}"

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(255), default="")
    username: Mapped[Optional[str]] = mapped_column(String(255), default="")
    joined_at: Mapped[str] = mapped_column(String(64))  # ISO 8601 (UTC)

engine = create_async_engine(ASYNC_DB_URL, echo=False, future=True)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def upsert_user(user_id: int, first_name: Optional[str], username: Optional[str]) -> None:
    async with AsyncSessionLocal() as session:
        stmt = sqlite_insert(User).values(
            user_id=user_id,
            first_name=first_name or "",
            username=username or "",
            joined_at=datetime.now(timezone.utc).isoformat()
        ).on_conflict_do_update(
            index_elements=[User.user_id],
            set_=dict(first_name=(first_name or ""), username=(username or ""))
        )
        await session.execute(stmt)
        await session.commit()

async def get_all_user_ids() -> list[int]:
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User.user_id))
        return list(res.scalars().all())

# ---------- FSM ----------
class Delivery(StatesGroup):
    waiting_for_content = State()

# ---------- Клавиатуры ----------
def start_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Получить доступ", callback_data="get_access")
    ]])

def subscribe_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Перейти к каналу", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")],
        [InlineKeyboardButton(text="Подписался", callback_data="check_sub")]
    ])

# ---------- Вспомогательное ----------
def target_chat_id() -> int | str:
    # используем numeric id если он указан (надежнее), иначе username
    if CHANNEL_ID_RAW:
        try:
            return int(CHANNEL_ID_RAW)  # -100...
        except ValueError:
            pass
    return CHANNEL_USERNAME

# ---------- Проверка подписки ----------
async def is_subscribed(bot: Bot, user_id: int) -> bool:
    """
    Требуется: бот добавлен в канал и сделан админом.
    Статусы, которые считаем подпиской: member / administrator / creator.
    """
    try:
        member = await bot.get_chat_member(chat_id=target_chat_id(), user_id=user_id)
        status = getattr(member, "status", "")
        return str(status) in {"member", "administrator", "creator"}
    except TelegramBadRequest as e:
        # Частые причины: бот не админ, канал приватный и id/username неверен
        # Можно залогировать e.message при необходимости
        return False
    except TelegramAPIError:
        return False

# ---------- Хендлеры ----------
async def on_start(message: Message, bot: Bot):
    await upsert_user(message.from_user.id, message.from_user.first_name, message.from_user.username)
    text = (
        "👋 Приветствую!\n\n"
        "Нажмите кнопку ниже, чтобы получить доступ."
    )
    await message.answer(text, reply_markup=start_kb())

async def on_get_access(callback: CallbackQuery):
    await upsert_user(callback.from_user.id, callback.from_user.first_name, callback.from_user.username)
    text = (
        "Чтобы получить доступ к приватному чату, подпишитесь на канал и затем нажмите кнопку «Подписался»."
    )
    await callback.message.edit_text(text, reply_markup=subscribe_kb())
    await callback.answer()

async def on_check_sub(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    if await is_subscribed(bot, user_id):
        await upsert_user(user_id, callback.from_user.first_name, callback.from_user.username)
        await callback.message.edit_text(
            "✅ Подписка подтверждена!\n"
            f"Ваша ссылка на приватный чат:\n{PRIVATE_CHAT_LINK}"
        )
    else:
        await callback.answer(
            "Ошибка: вы не подписаны на канал. Убедитесь, что подписались и попробуйте снова.\n",
            show_alert=True
        )

async def cmd_delivery_start(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return await message.reply("⛔️ Команда доступна только администратору.")
    await state.set_state(Delivery.waiting_for_content)
    await message.reply(
        "✉️ Режим рассылки.\n"
        "Отправьте сообщение, которое нужно разослать всем пользователям.\n\n"
        "Можно отправить текст/фото/видео/документ — будет разослано «как есть»."
    )

async def handle_delivery_content(message: Message, bot: Bot, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()

    user_ids = await get_all_user_ids()
    if not user_ids:
        return await message.reply("Список пользователей пуст — рассылать некому.")

    sent = failed = 0
    for uid in user_ids:
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=message.chat.id, message_id=message.message_id)
            sent += 1
            await asyncio.sleep(0.03)
        except TelegramAPIError:
            failed += 1
            await asyncio.sleep(0.03)

    await message.reply(f"🚀 Готово!\nУспешно: {sent}\nОшибок: {failed}")

# ---------- Регистрация ----------
def register_handlers(dp: Dispatcher):
    dp.message.register(on_start, CommandStart())
    dp.callback_query.register(on_get_access, F.data == "get_access")
    dp.callback_query.register(on_check_sub, F.data == "check_sub")
    dp.message.register(cmd_delivery_start, Command("delivery"))
    dp.message.register(handle_delivery_content, Delivery.waiting_for_content)

# ---------- Entrypoint ----------
async def main():
    if not BOT_TOKEN:
        base_dir = Path(__file__).resolve().parent
        raise RuntimeError(
            "Не найден BOT_TOKEN. Создайте .env рядом с main.py:\n"
            "BOT_TOKEN=1234567890:AAExampleTokenFromBotFather\n"
            f"CHANNEL_ID={CHANNEL_ID_RAW}\nCHANNEL_USERNAME={CHANNEL_USERNAME}\n"
            f"Каталог: {base_dir}\n"
        )

    await init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    register_handlers(dp)

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
