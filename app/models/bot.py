"""Minimal bot state, chat intake and transactional outgoing messages."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BotReceipt(Base):
    __tablename__ = "bot_receipts"

    event_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BotConversation(Base):
    __tablename__ = "bot_conversations"

    max_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    state: Mapped[str] = mapped_column(String(32), default="menu", server_default="menu")
    data: Mapped[dict[str, str]] = mapped_column(
        JSONB, default=dict, server_default=sql_text("'{}'")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_timestamp_ms: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")


class HouseChat(Base):
    __tablename__ = "house_chats"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "house_id"], ["houses.organization_id", "houses.id"]
        ),
        ForeignKeyConstraint(
            ["organization_id", "category_id"], ["categories.organization_id", "categories.id"]
        ),
    )

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    house_id: Mapped[UUID] = mapped_column()
    category_id: Mapped[UUID] = mapped_column()


class BotOrganizationSettings(Base):
    __tablename__ = "bot_organization_settings"

    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), primary_key=True)
    group_analysis_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sql_text("true")
    )


class BotGroupChat(Base):
    __tablename__ = "bot_group_chats"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    analysis_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sql_text("true")
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChatSignal(Base):
    __tablename__ = "chat_signals"
    __table_args__ = (
        Index("ix_chat_signals_chat_actor_created", "chat_id", "actor_max_user_id", "created_at"),
        Index("ix_chat_signals_fingerprint", "fingerprint"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_key: Mapped[str] = mapped_column(String(64), unique=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("bot_group_chats.chat_id"), index=True)
    actor_max_user_id: Mapped[int] = mapped_column(BigInteger)
    fingerprint: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16))
    problem: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatObservation(Base):
    __tablename__ = "chat_observations"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_key: Mapped[str] = mapped_column(String(64), unique=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("house_chats.chat_id"))
    text: Mapped[str] = mapped_column(Text)
    important: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sql_text("false")
    )
    dismissed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sql_text("false")
    )
    request_id: Mapped[UUID | None] = mapped_column(ForeignKey("requests.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BotDelivery(Base):
    __tablename__ = "bot_deliveries"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    sequence: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    max_user_id: Mapped[int] = mapped_column(BigInteger)
    access_stamp: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text, default="", server_default="")
    buttons: Mapped[list[list[dict[str, str]]] | None] = mapped_column(JSONB)
    callback_id: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending", index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_id: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
