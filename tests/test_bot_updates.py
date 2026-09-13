"""Offline MAX Update fixtures verify identity, privacy and stable deduplication."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from app.bot.updates import normalize_update


def user(user_id: int = 123, *, is_bot: bool = False) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "first_name": "Иван",
        "last_name": "Петров",
        "is_bot": is_bot,
        "last_activity_time": 0,
    }


def created() -> dict[str, Any]:
    return {
        "update_type": "message_created",
        "timestamp": 1700000000000,
        "message": {
            "sender": user(),
            "recipient": {"chat_id": -456, "chat_type": "dialog", "user_id": 900},
            "timestamp": 1700000000000,
            "body": {"mid": "synthetic.mid.1", "seq": 1, "text": "/start", "attachments": []},
        },
        "user_locale": "ru-RU",
    }


def callback() -> dict[str, Any]:
    message = created()["message"]
    message["sender"] = user(900, is_bot=True)
    message["recipient"]["user_id"] = 123
    message["body"]["text"] = "Заявка №42"
    return {
        "update_type": "message_callback",
        "timestamp": 1700000000010,
        "callback": {
            "user": user(),
            "timestamp": 1700000000005,
            "callback_id": "keyboard-1",
            "payload": "requests:list",
        },
        "message": message,
    }


def started() -> dict[str, Any]:
    return {
        "update_type": "bot_started",
        "timestamp": 1700000000000,
        "chat_id": -456,
        "user": user(),
        "payload": "ignored-deeplink-payload",
    }


def digest(parts: list[str | int]) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


def test_created_normalizes_documented_identity_and_personal_recipient() -> None:
    update = normalize_update("staff", created())
    assert update is not None
    assert update.namespace == "staff"
    assert update.update_type == "message_created"
    assert update.actor_id == 123
    assert update.actor_name == "Иван Петров"
    assert update.is_bot is False
    assert update.chat_id == -456
    assert update.is_private is True
    assert update.text == "/start"
    assert update.message_id == "synthetic.mid.1"
    assert update.callback_id is None
    assert update.callback_payload is None
    assert update.timestamp_ms == 1700000000000
    assert update.event_key == digest(["staff", "message_created", -456, "synthetic.mid.1"])


def test_callback_uses_clicker_identity_instead_of_original_bot_author() -> None:
    update = normalize_update("staff", callback())
    assert update is not None
    assert update.actor_id == 123
    assert update.is_bot is False
    assert update.is_private is True
    assert update.callback_id == "keyboard-1"
    assert update.callback_payload == "requests:list"
    assert update.timestamp_ms == 1700000000005
    assert update.event_key == digest(
        ["staff", "message_callback", "keyboard-1", 123, 1700000000005]
    )


def test_callback_does_not_use_or_require_original_sender_profile() -> None:
    payload = callback()
    payload["message"]["sender"] = {"user_id": "spoofed-actor", "first_name": None}
    update = normalize_update("staff", payload)
    assert update is not None
    assert update.actor_id == 123
    assert update.actor_name == "Иван Петров"


def test_bot_started_is_personal_and_does_not_treat_deeplink_as_command() -> None:
    update = normalize_update("staff", started())
    assert update is not None
    assert update.is_private is True
    assert update.actor_id == 123
    assert update.chat_id == -456
    assert update.text is None
    assert update.callback_payload is None
    assert update.event_key == digest(["staff", "bot_started", -456, 123, 1700000000000])


@pytest.mark.parametrize("chat_type", ["chat", "channel"])
@pytest.mark.parametrize("factory", [created, callback])
def test_group_messages_and_callbacks_never_become_private(chat_type: str, factory: Any) -> None:
    payload = factory()
    payload["message"]["recipient"]["chat_type"] = chat_type
    update = normalize_update("staff", payload)
    assert update is not None
    assert update.is_private is False


def test_repeated_message_has_same_key_after_text_profile_or_delivery_timestamp_change() -> None:
    payload = created()
    original = normalize_update("staff", payload)
    changed = deepcopy(payload)
    changed["message"]["body"]["text"] = "Corrected source text"
    changed["message"]["sender"]["first_name"] = "Пётр"
    changed["message"]["sender"]["last_activity_time"] = 999
    changed["timestamp"] += 500
    changed["unknown_field"] = "must not affect identity"
    repeated = normalize_update("staff", changed)
    assert original is not None and repeated is not None
    assert original.event_key == repeated.event_key


def test_callback_repeat_ignores_profile_text_and_payload_but_distinguishes_click_time() -> None:
    payload = callback()
    original = normalize_update("staff", payload)
    changed = deepcopy(payload)
    changed["message"]["body"]["text"] = "Updated bot card"
    changed["callback"]["user"]["first_name"] = "Пётр"
    changed["callback"]["payload"] = "menu"
    changed["timestamp"] += 500
    repeated = normalize_update("staff", changed)
    changed["callback"]["timestamp"] += 1
    next_click = normalize_update("staff", changed)
    assert original is not None and repeated is not None and next_click is not None
    assert original.event_key == repeated.event_key
    assert next_click.event_key != original.event_key


@pytest.mark.parametrize("factory", [created, callback, started])
def test_bot_namespaces_have_distinct_event_keys(factory: Any) -> None:
    staff = normalize_update("staff", factory())
    observer = normalize_update("observer", factory())
    assert staff is not None and observer is not None
    assert staff.event_key != observer.event_key


def test_message_identifiers_are_scoped_to_chat() -> None:
    payload = created()
    original = normalize_update("staff", payload)
    payload["message"]["recipient"]["chat_id"] = -457
    another_chat = normalize_update("staff", payload)
    assert original is not None and another_chat is not None
    assert original.event_key != another_chat.event_key


@pytest.mark.parametrize("factory", [callback, started])
def test_actor_is_part_of_callback_and_start_identity(factory: Any) -> None:
    payload = factory()
    original = normalize_update("staff", payload)
    actor = payload["callback"]["user"] if "callback" in payload else payload["user"]
    actor["user_id"] += 1
    another_actor = normalize_update("staff", payload)
    assert original is not None and another_actor is not None
    assert original.event_key != another_actor.event_key


@pytest.mark.parametrize("factory", [created, callback, started])
def test_events_authored_by_bots_are_ignored(factory: Any) -> None:
    payload = factory()
    if "callback" in payload:
        payload["callback"]["user"]["is_bot"] = True
    elif "message" in payload:
        payload["message"]["sender"]["is_bot"] = True
    else:
        payload["user"]["is_bot"] = True
    assert normalize_update("staff", payload) is None


@pytest.mark.parametrize("body", [None, {}])
def test_created_without_message_identity_is_ignored(body: object) -> None:
    payload = created()
    payload["message"]["body"] = body
    assert normalize_update("staff", payload) is None


def test_non_text_message_with_mid_remains_valid() -> None:
    payload = created()
    payload["message"]["body"]["text"] = None
    update = normalize_update("observer", payload)
    assert update is not None
    assert update.text is None
    assert update.message_id == "synthetic.mid.1"


def test_callback_with_null_body_keeps_verified_recipient_context() -> None:
    payload = callback()
    payload["message"]["body"] = None
    update = normalize_update("staff", payload)
    assert update is not None
    assert update.message_id is None
    assert update.text is None
    assert update.is_private is True


@pytest.mark.parametrize("missing", [False, True])
def test_callback_without_original_message_is_ignored(missing: bool) -> None:
    payload = callback()
    if missing:
        del payload["message"]
    else:
        payload["message"] = None
    assert normalize_update("staff", payload) is None


@pytest.mark.parametrize("missing", [False, True])
def test_callback_payload_can_be_null_or_omitted(missing: bool) -> None:
    payload = callback()
    if missing:
        del payload["callback"]["payload"]
    else:
        payload["callback"]["payload"] = None
    update = normalize_update("staff", payload)
    assert update is not None
    assert update.callback_payload is None


@pytest.mark.parametrize("value", [True, 0, -1, 2**63, 1.0, "123", None])
@pytest.mark.parametrize("factory", [created, callback, started])
def test_actor_id_is_a_positive_strict_signed_integer(value: object, factory: Any) -> None:
    payload = factory()
    if "callback" in payload:
        payload["callback"]["user"]["user_id"] = value
    elif "message" in payload:
        payload["message"]["sender"]["user_id"] = value
    else:
        payload["user"]["user_id"] = value
    assert normalize_update("staff", payload) is None


@pytest.mark.parametrize("value", [True, -(2**63) - 1, 2**63, 1.0, "-456", None])
@pytest.mark.parametrize("factory", [created, started])
def test_chat_id_must_be_a_strict_signed_integer(value: object, factory: Any) -> None:
    payload = factory()
    if "message" in payload:
        payload["message"]["recipient"]["chat_id"] = value
    else:
        payload["chat_id"] = value
    assert normalize_update("staff", payload) is None


@pytest.mark.parametrize("value", [True, -1, 2**63, 1.0, "123", None])
def test_update_timestamp_is_a_strict_nonnegative_signed_integer(value: object) -> None:
    payload = created()
    payload["timestamp"] = value
    assert normalize_update("staff", payload) is None


@pytest.mark.parametrize("value", [True, -1, 2**63, 1.0, "123", None])
def test_callback_requires_its_own_valid_click_timestamp(value: object) -> None:
    payload = callback()
    payload["callback"]["timestamp"] = value
    assert normalize_update("staff", payload) is None


@pytest.mark.parametrize("chat_id", [-(2**63), 0, 2**63 - 1])
def test_signed_chat_boundaries_and_timestamp_zero_are_accepted(chat_id: int) -> None:
    payload = created()
    payload["message"]["recipient"]["chat_id"] = chat_id
    payload["timestamp"] = 0
    update = normalize_update("staff", payload)
    assert update is not None
    assert update.chat_id == chat_id
    assert update.timestamp_ms == 0


@pytest.mark.parametrize(
    "payload", [None, [], "update", True, {}, {"update_type": "message_edited"}]
)
def test_unknown_or_malformed_top_level_payloads_are_ignored(payload: object) -> None:
    assert normalize_update("staff", payload) is None


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("message", "sender", "first_name"), None),
        (("message", "sender", "last_name"), 123),
        (("message", "sender", "is_bot"), "false"),
        (("message", "sender", "is_bot"), 0),
        (("message", "recipient", "chat_type"), "private"),
        (("message", "recipient"), None),
        (("message", "body", "mid"), " "),
        (("message", "body", "mid"), 123),
        (("message", "body", "text"), True),
        (("message", "body", "text"), "\ud800"),
    ],
)
def test_malformed_nested_fields_are_ignored(path: tuple[str, ...], value: object) -> None:
    payload = created()
    node = payload
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value
    assert normalize_update("staff", payload) is None


@pytest.mark.parametrize("field", ["sender", "recipient", "body"])
def test_created_required_routing_fields_cannot_be_missing(field: str) -> None:
    payload = created()
    del payload["message"][field]
    assert normalize_update("staff", payload) is None


@pytest.mark.parametrize("callback_id", ["", " ", 123, None])
def test_callback_id_is_nonempty_and_strict(callback_id: object) -> None:
    payload = callback()
    payload["callback"]["callback_id"] = callback_id
    assert normalize_update("staff", payload) is None


def test_text_and_callback_payload_have_explicit_limits() -> None:
    message = created()
    message["message"]["body"]["text"] = "А" * 10000
    assert normalize_update("observer", message) is not None
    message["message"]["body"]["text"] += "А"
    assert normalize_update("observer", message) is None
    click = callback()
    click["callback"]["payload"] = "Я" * 1024
    assert normalize_update("staff", click) is not None
    click["callback"]["payload"] += "Я"
    assert normalize_update("staff", click) is None


def test_display_name_is_bounded_and_unknown_fields_are_ignored() -> None:
    payload = created()
    payload["message"]["sender"]["first_name"] = "  И" * 200
    payload["message"]["sender"]["last_name"] = None
    payload["message"]["sender"]["unknown_profile_field"] = {"must": "be ignored"}
    update = normalize_update("staff", payload)
    assert update is not None
    assert len(update.actor_name) == 200
    assert "  " not in update.actor_name


def test_normalized_updates_are_immutable_and_repr_omits_private_content() -> None:
    payload = created()
    payload["message"]["body"]["text"] = "private customer issue"
    update = normalize_update("staff", payload)
    assert update is not None
    with pytest.raises(FrozenInstanceError):
        update.text = "changed"  # type: ignore[misc]
    assert "private customer issue" not in repr(update)
    assert "Иван" not in repr(update)
    assert "synthetic.mid.1" not in repr(update)


def test_unknown_bot_namespace_is_ignored() -> None:
    assert normalize_update("unknown", created()) is None  # type: ignore[arg-type]
