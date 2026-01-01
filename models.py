# models.py
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    DateTime,
    ForeignKey,
    func
)
from datetime import datetime


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False)
    username = Column(String)
    first_name = Column(String)
    created_at = Column(DateTime, server_default=func.now())

    messages = relationship("Message", back_populates="user")


class MessageHistory(Base):
    __tablename__ = "message_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int]
    role: Mapped[str]
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        default=datetime.utcnow
    )


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int]
    service: Mapped[str]
    date: Mapped[str]
    time: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(
        default=datetime.utcnow
    )
from sqlalchemy import ForeignKey, Text
from datetime import datetime
from sqlalchemy import DateTime

class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="messages")
