"""Project the supported MAX updates into a small, stable bot input.

Payload fields follow the official MAX Update/Message/User contracts, checked on
2026-09-13. This module parses data only; webhook authenticity must be verified by
the caller before any normalized input is allowed to perform business actions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

Namespace = Literal["staff", "observer"]
_UserID = Annotated[int, Field(gt=0, lt=2**63)]
_ChatID = Annotated[int, Field(ge=-(2**63), lt=2**63)]
_Timestamp = Annotated[int, Field(ge=0, lt=2**63)]


@dataclass(frozen=True, slots=True)
class IncomingUpdate:
    namespace: Namespace
    event_key: str
    update_type: str
    actor_id: int = field(repr=False)
    actor_name: str = field(repr=False)
    is_bot: bool
    chat_id: int = field(repr=False)
    is_private: bool
    text: str | None = field(repr=False)
    message_id: str | None = field(repr=False)
    callback_id: str | None = field(repr=False)
    callback_payload: str | None = field(repr=False)
    timestamp_ms: int


class _Projection(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True, frozen=True)

    @field_validator("*")
    @classmethod
    def reject_invalid_unicode(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeError:
                raise ValueError("Invalid string encoding") from None
        return value


class _User(_Projection):
    user_id: _UserID
    first_name: str
    last_name: str | None = None
    is_bot: bool

    @property
    def display_name(self) -> str:
        return " ".join(f"{self.first_name} {self.last_name or ''}".split())[:200]


class _Recipient(_Projection):
    chat_id: _ChatID
    chat_type: Literal["dialog", "chat", "channel"]


class _MessageBody(_Projection):
    mid: str = Field(min_length=1)
    text: str | None = Field(default=None, max_length=10000)

    @field_validator("mid")
    @classmethod
    def require_message_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Missing message identifier")
        return value


class _Message(_Projection):
    recipient: _Recipient
    body: _MessageBody | None = None


class _AuthoredMessage(_Message):
    sender: _User | None = None


class _Callback(_Projection):
    user: _User
    timestamp: _Timestamp
    callback_id: str = Field(min_length=1)
    payload: str | None = Field(default=None, max_length=1024)

    @field_validator("callback_id")
    @classmethod
    def require_callback_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Missing callback identifier")
        return value


class _MessageCreated(_Projection):
    update_type: Literal["message_created"]
    timestamp: _Timestamp
    message: _AuthoredMessage


class _MessageCallback(_Projection):
    update_type: Literal["message_callback"]
    timestamp: _Timestamp
    callback: _Callback
    message: _Message | None


class _BotStarted(_Projection):
    update_type: Literal["bot_started"]
    timestamp: _Timestamp
    chat_id: _ChatID
    user: _User


_Update = Annotated[
    _MessageCreated | _MessageCallback | _BotStarted,
    Field(discriminator="update_type"),
]
_update_adapter: TypeAdapter[_MessageCreated | _MessageCallback | _BotStarted] = TypeAdapter(
    _Update
)


def _event_key(parts: list[str | int]) -> str:
    serialized = json.dumps(parts, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def normalize_update(namespace: Namespace, payload: object) -> IncomingUpdate | None:
    """Ignore unsupported, bot-authored or malformed updates without logging data.

    Keys use documented identifiers; profile and text changes cannot turn a
    repeated delivery into a second business action. Callbacks additionally use
    the click timestamp because MAX describes callback_id as the keyboard ID.
    """
    if namespace not in ("staff", "observer"):
        return None
    try:
        update = _update_adapter.validate_python(payload)
    except ValidationError:
        return None

    text: str | None = None
    message_id: str | None = None
    callback_id: str | None = None
    callback_payload: str | None = None
    timestamp = update.timestamp
    if isinstance(update, _MessageCreated):
        message = update.message
        if message.sender is None or message.body is None:
            # A body-less forwarded message has no mid for reliable deduplication.
            return None
        actor = message.sender
        chat_id = message.recipient.chat_id
        is_private = message.recipient.chat_type == "dialog"
        message_id = message.body.mid
        text = message.body.text
        key_parts: list[str | int] = [namespace, update.update_type, chat_id, message_id]
    elif isinstance(update, _MessageCallback):
        if update.message is None:
            # A removed original message cannot establish private-chat context.
            return None
        actor = update.callback.user
        chat_id = update.message.recipient.chat_id
        is_private = update.message.recipient.chat_type == "dialog"
        if update.message.body is not None:
            message_id = update.message.body.mid
            text = update.message.body.text
        callback_id = update.callback.callback_id
        callback_payload = update.callback.payload
        timestamp = update.callback.timestamp
        key_parts = [namespace, update.update_type, callback_id, actor.user_id, timestamp]
    else:
        actor = update.user
        chat_id = update.chat_id
        is_private = True
        key_parts = [namespace, update.update_type, chat_id, actor.user_id, timestamp]

    if actor.is_bot or not actor.display_name:
        return None
    return IncomingUpdate(
        namespace=namespace,
        event_key=_event_key(key_parts),
        update_type=update.update_type,
        actor_id=actor.user_id,
        actor_name=actor.display_name,
        is_bot=False,
        chat_id=chat_id,
        is_private=is_private,
        text=text,
        message_id=message_id,
        callback_id=callback_id,
        callback_payload=callback_payload,
        timestamp_ms=timestamp,
    )
