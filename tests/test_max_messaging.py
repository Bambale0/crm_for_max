"""Synthetic offline fixtures for MAX personal messages and callbacks."""

from __future__ import annotations

import json
import logging
import traceback
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from app.integrations.max.messaging import (
    CallbackButton,
    MaxDeliveryError,
    MaxMessagingClient,
)

TOKEN = "synthetic-send-token-do-not-use"
MESSAGE = {"message": {"body": {"mid": "synthetic.mid.123", "text": "Private CRM text"}}}


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> MaxMessagingClient:
    return MaxMessagingClient(
        token=SecretStr(TOKEN), timeout_seconds=3, transport=httpx.MockTransport(handler)
    )


async def test_send_uses_personal_recipient_plain_text_and_documented_callback_keyboard(
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx.Request] = []
    private_text = "Заявка № 42: <b>Течёт кран</b> https://example.invalid/private"
    buttons = [[CallbackButton("Мои заявки", "requests:list")], [CallbackButton("Меню", "menu")]]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.scheme == "https"
        assert request.url.host == "platform-api2.max.ru"
        assert request.url.path == "/messages"
        assert dict(request.url.params) == {"user_id": "123", "disable_link_preview": "true"}
        assert request.headers["Authorization"] == TOKEN
        assert request.extensions["timeout"]["read"] == 3
        assert json.loads(request.content) == {
            "text": private_text,
            "attachments": [
                {
                    "type": "inline_keyboard",
                    "payload": {
                        "buttons": [
                            [
                                {
                                    "type": "callback",
                                    "text": "Мои заявки",
                                    "payload": "requests:list",
                                }
                            ],
                            [{"type": "callback", "text": "Меню", "payload": "menu"}],
                        ]
                    },
                }
            ],
        }
        return httpx.Response(200, json=MESSAGE)

    caplog.set_level(logging.DEBUG)
    async with make_client(handler) as client:
        sent = await client.send_text(123, private_text, buttons)
        assert TOKEN not in repr(client)
    assert sent.message_id == "synthetic.mid.123"
    assert len(requests) == 1
    assert TOKEN not in caplog.text
    assert private_text not in caplog.text
    assert "Private CRM text" not in repr(sent)
    assert "requests:list" not in repr(buttons)


async def test_text_at_documented_limit_has_no_attachments_or_format() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"text": "Я" * 4000}
        return httpx.Response(200, json=MESSAGE)

    async with make_client(handler) as client:
        await client.send_text(2**63 - 1, "Я" * 4000)


async def test_callback_acknowledgment_does_not_modify_original_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/answers"
        assert dict(request.url.params) == {"callback_id": "callback:/?& id"}
        assert request.headers["Authorization"] == TOKEN
        assert json.loads(request.content) == {}
        return httpx.Response(200, json={"success": True})

    async with make_client(handler) as client:
        assert await client.answer_callback("callback:/?& id") is None


@pytest.mark.parametrize(
    ("status", "code", "uncertain"),
    [
        (400, "http_error", False),
        (401, "unauthorized", False),
        (403, "access_denied", False),
        (404, "not_found", False),
        (429, "rate_limited", False),
        (500, "upstream_unavailable", True),
        (503, "upstream_unavailable", True),
        (302, "unexpected_redirect", True),
        (307, "unexpected_redirect", True),
        (201, "http_error", True),
    ],
)
async def test_http_failures_distinguish_rejection_from_uncertain_delivery_without_retry(
    status: int, code: str, uncertain: bool, caplog: pytest.LogCaptureFixture
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status,
            text=f"sensitive provider text {TOKEN}",
            headers={"Location": f"https://example.invalid/{TOKEN}", "Retry-After": "2"},
        )

    caplog.set_level(logging.DEBUG)
    async with make_client(handler) as client:
        with pytest.raises(MaxDeliveryError) as caught:
            await client.send_text(123, "Заявка")
    assert caught.value.code == code
    assert caught.value.status_code == status
    assert caught.value.delivery_uncertain is uncertain
    assert caught.value.__context__ is None
    assert len(requests) == 1
    rendered = "".join(traceback.format_exception(caught.value)) + caplog.text
    assert TOKEN not in rendered
    assert "sensitive provider text" not in rendered
    assert not hasattr(caught.value, "request")
    assert not hasattr(caught.value, "response")


@pytest.mark.parametrize(
    ("error_type", "code"),
    [
        (httpx.ReadTimeout, "timeout"),
        (httpx.WriteTimeout, "timeout"),
        (httpx.ConnectError, "network_error"),
    ],
)
async def test_transport_failure_never_retries_a_potentially_accepted_message(
    error_type: type[httpx.RequestError], code: str
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise error_type(f"Sensitive TLS or timeout context: {TOKEN}", request=request)

    async with make_client(handler) as client:
        with pytest.raises(MaxDeliveryError) as caught:
            await client.send_text(123, "Заявка")
    assert attempts == 1
    assert caught.value.code == code
    assert caught.value.delivery_uncertain is True
    assert caught.value.__context__ is None
    assert TOKEN not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"message": None},
        {"message": {"body": {"mid": 123}}},
        {"message": {"body": {"mid": ""}}},
    ],
)
async def test_malformed_success_is_uncertain_without_echoing_payload(payload: object) -> None:
    async with make_client(lambda _: httpx.Response(200, json=payload)) as client:
        with pytest.raises(MaxDeliveryError, match="invalid_response") as caught:
            await client.send_text(123, "Заявка")
    assert caught.value.delivery_uncertain is True
    assert caught.value.__context__ is None


async def test_invalid_json_is_uncertain_and_hides_raw_response() -> None:
    async with make_client(lambda _: httpx.Response(200, text=TOKEN)) as client:
        with pytest.raises(MaxDeliveryError, match="invalid_response") as caught:
            await client.send_text(123, "Заявка")
    assert caught.value.delivery_uncertain is True
    assert caught.value.__context__ is None
    assert TOKEN not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("payload", [{}, {"success": 1}, {"success": "true"}, None])
async def test_callback_success_must_be_a_boolean(payload: object) -> None:
    async with make_client(lambda _: httpx.Response(200, json=payload)) as client:
        with pytest.raises(MaxDeliveryError, match="invalid_response") as caught:
            await client.answer_callback("callback-1")
    assert caught.value.delivery_uncertain is True


async def test_callback_rejection_is_explicit_and_sanitized() -> None:
    async with make_client(
        lambda _: httpx.Response(200, json={"success": False, "message": TOKEN})
    ) as client:
        with pytest.raises(MaxDeliveryError, match="callback_rejected") as caught:
            await client.answer_callback("callback-1")
    assert caught.value.delivery_uncertain is False
    assert caught.value.status_code == 200
    assert TOKEN not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("user_id", [True, 0, -1, 2**63, "123", "../chats/-123"])
async def test_invalid_or_group_recipient_never_sends_request(user_id: object) -> None:
    async with make_client(lambda _: pytest.fail("No HTTP request expected")) as client:
        with pytest.raises(MaxDeliveryError, match="invalid_request"):
            await client.send_text(user_id, "Заявка")  # type: ignore[arg-type]


@pytest.mark.parametrize("text", ["", "\t\n", "Я" * 4001, "\ud800", None])
async def test_invalid_text_never_sends_request(text: object) -> None:
    async with make_client(lambda _: pytest.fail("No HTTP request expected")) as client:
        with pytest.raises(MaxDeliveryError, match="invalid_request"):
            await client.send_text(123, text)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "payload"),
    [("", "menu"), ("A" * 129, "menu"), ("Меню", ""), ("Меню", "a" * 1025), ("Меню", "\ud800")],
)
def test_invalid_callback_button_is_rejected(text: str, payload: str) -> None:
    with pytest.raises(MaxDeliveryError, match="invalid_request"):
        CallbackButton(text, payload)


@pytest.mark.parametrize(
    "buttons",
    [
        [],
        [[]],
        [[CallbackButton("Меню", "menu")] * 8],
        [[CallbackButton("Меню", "menu")]] * 31,
        [["menu"]],
        [None],
        123,
        "menu",
    ],
)
async def test_invalid_keyboard_never_sends_request(buttons: object) -> None:
    async with make_client(lambda _: pytest.fail("No HTTP request expected")) as client:
        with pytest.raises(MaxDeliveryError, match="invalid_request"):
            await client.send_text(123, "Заявка", buttons)  # type: ignore[arg-type]


@pytest.mark.parametrize("callback_id", ["", " ", "\ud800", None])
async def test_invalid_callback_identifier_never_sends_request(callback_id: object) -> None:
    async with make_client(lambda _: pytest.fail("No HTTP request expected")) as client:
        with pytest.raises(MaxDeliveryError, match="invalid_request"):
            await client.answer_callback(callback_id)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "base_url",
    [
        "http://platform-api2.max.ru",
        "https://example.invalid",
        f"https://{TOKEN}@platform-api2.max.ru",
        "https://platform-api2.max.ru:8443",
        f"https://platform-api2.max.ru?access_token={TOKEN}",
    ],
)
def test_invalid_origin_cannot_receive_authorization(base_url: str) -> None:
    with pytest.raises(MaxDeliveryError, match="invalid_configuration") as caught:
        MaxMessagingClient(token=SecretStr(TOKEN), base_url=base_url)
    assert caught.value.__context__ is None
    assert TOKEN not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_invalid_timeout_is_rejected(timeout: float) -> None:
    with pytest.raises(MaxDeliveryError, match="invalid_configuration"):
        MaxMessagingClient(token=SecretStr(TOKEN), timeout_seconds=timeout)


@pytest.mark.parametrize("token", ["", " ", "\r\nsecret", "секрет"])
def test_invalid_token_is_rejected(token: str) -> None:
    with pytest.raises(MaxDeliveryError, match="invalid_configuration"):
        MaxMessagingClient(token=SecretStr(token))


def test_client_enables_tls_verification_and_disables_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def capture(**kwargs: object) -> object:
        observed.update(kwargs)
        return object()

    monkeypatch.setattr(httpx, "AsyncClient", capture)
    MaxMessagingClient(token=SecretStr(TOKEN))
    assert observed["verify"] is True
    assert observed["follow_redirects"] is False
    assert observed["base_url"] == "https://platform-api2.max.ru"
    assert "headers" not in observed
