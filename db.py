# -*- coding: utf-8 -*-

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select
from models import Base, User, Message
import os

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import select

from models import Base, User, MessageHistory, Appointment


# === DATABASE URL ===

DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL and DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace(
        "postgresql://",
        "postgresql+asyncpg://",
        1
    )

engine = create_async_engine(
    DATABASE_URL,
    echo=False
)

AsyncSessionLocal = sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)
# === ENGINE ===
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
)

# === SESSION ===
async_session = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


# === INIT DB ===
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# === USERS ===
async def get_or_create_user(
    telegram_id: int,
    username: str | None,
    first_name: str | None,
):
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()

        if not user:
            user = User(
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
            )
            session.add(user)
            await session.commit()

        return user


# === SAVE MESSAGE ===
async def save_message(
    user_id: int,
    role: str,
    content: str,
):
    async with async_session() as session:
        msg = MessageHistory(
            user_id=user_id,
            role=role,
            content=content,
        )
        session.add(msg)
        await session.commit()

# === GET HISTORY MESSAGES ===

async def get_last_messages(user_id: int, limit: int = 10):
    async with async_session() as session:
        result = await session.execute(
            select(Message)
            .where(Message.user_id == user_id)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        messages = result.scalars().all()
        return list(reversed(messages))

# === GET HISTORY ===
async def get_history(user_id: int, limit: int = 10):
    async with async_session() as session:
        result = await session.execute(
            select(MessageHistory)
            .where(MessageHistory.user_id == user_id)
            .order_by(MessageHistory.created_at.asc())
            .limit(limit)
        )

        messages = result.scalars().all()

        return [
            {"role": m.role, "content": m.content}
            for m in messages
        ]
