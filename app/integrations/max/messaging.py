"""Personal MAX messages and callback acknowledgments, without automatic retries.

Contracts checked against dev.max.ru/docs-api on 2026-09-13:
POST/messages, POST/answers, Message and the embedded OpenAPI schema. The current
CallbackAnswer schema has no notification field; acknowledgments send an empty
body and never edit the original message.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Literal, Self

import httpx
from pydantic import SecretStr

from app.integrations.max.client import MAX_API_BASE_URL, MaxAPIError, _validate_base_url

MAX_TEXT_LENGTH = 4000
MAX_BUTTON_TEXT_LENGTH = 128
MAX_CALLBACK_PAYLOAD_LENGTH = 1024
MAX_BUTTON_ROWS = 30
MAX_BUTTONS_PER_ROW = 7

DeliveryErrorCode = Literal[
    "invalid_configuration",
    "invalid_request",
    "unauthorized",
    "access_denied",
    "not_found",
    "rate_limited",
    "upstream_unavailable",
    "unexpected_redirect",
    "http_error",
    "timeout",
    "network_error",
    "invalid_response",
    "callback_rejected",
]


class MaxDeliveryError(Exception):
    """Safe diagnostics with an explicit indication of ambiguous delivery.

    No request, response, provider text or underlying exception is retained.
    A timeout or malformed success can occur after MAX accepted the message;
    callers must not blindly resend when delivery_uncertain is true.
    """

    def __init__(
        self,
        code: DeliveryErrorCode,
        *,
        status_code: int | None = None,
        delivery_uncertain: bool = False,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.delivery_uncertain = delivery_uncertain
        super().__init__(f"MAX delivery: {code}")


def _valid_text(value: object, *, maximum: int | None = None) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    if maximum is not None and len(value) > maximum:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class CallbackButton:
    text: str = field(repr=False)
    payload: str = field(repr=False)

    def __post_init__(self) -> None:
        if not _valid_text(self.text, maximum=MAX_BUTTON_TEXT_LENGTH) or not _valid_text(
            self.payload, maximum=MAX_CALLBACK_PAYLOAD_LENGTH
        ):
            raise MaxDeliveryError("invalid_request")


@dataclass(frozen=True, slots=True)
class SentMessage:
    message_id: str = field(repr=False)


def _keyboard(buttons: Sequence[Sequence[CallbackButton]]) -> list[dict[str, object]]:
    if (
        not isinstance(buttons, Sequence)
        or isinstance(buttons, (str, bytes))
        or not 1 <= len(buttons) <= MAX_BUTTON_ROWS
    ):
        raise MaxDeliveryError("invalid_request")
    rows: list[list[dict[str, str]]] = []
    for row in buttons:
        if (
            not isinstance(row, Sequence)
            or isinstance(row, (str, bytes))
            or not 1 <= len(row) <= MAX_BUTTONS_PER_ROW
        ):
            raise MaxDeliveryError("invalid_request")
        result: list[dict[str, str]] = []
        for button in row:
            if not isinstance(button, CallbackButton):
                raise MaxDeliveryError("invalid_request")
            result.append({"type": "callback", "text": button.text, "payload": button.payload})
        rows.append(result)
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


class MaxMessagingClient:
    """TLS-verified MAX adapter restricted to personal user recipients.

    There is intentionally no chat_id argument. Rate scheduling belongs to the
    caller; this adapter performs exactly one HTTP attempt for each invocation.
    """

    def __init__(
        self,
        *,
        token: SecretStr,
        base_url: str = MAX_API_BASE_URL,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        valid_base = True
        try:
            _validate_base_url(base_url)
        except MaxAPIError:
            valid_base = False
        if (
            not valid_base
            or not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or not isinstance(token, SecretStr)
        ):
            raise MaxDeliveryError("invalid_configuration")
        value = token.get_secret_value()
        if not value or any(ord(character) < 33 or ord(character) > 126 for character in value):
            raise MaxDeliveryError("invalid_configuration")
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=MAX_API_BASE_URL,
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            verify=True,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._client.__aexit__(exc_type, exc_value, traceback)

    async def _post(
        self, path: str, *, params: dict[str, str | int | bool], body: dict[str, object]
    ) -> object:
        failure: DeliveryErrorCode | None = None
        response: httpx.Response | None = None
        try:
            response = await self._client.post(
                path,
                params=params,
                json=body,
                headers={"Authorization": self._token.get_secret_value()},
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            failure = "timeout"
        except httpx.RequestError:
            failure = "network_error"
        # Raising outside except prevents the original request/token surviving
        # through exception chaining, even when callers format a traceback.
        if failure is not None:
            raise MaxDeliveryError(failure, delivery_uncertain=True)
        assert response is not None

        code: DeliveryErrorCode | None = None
        status = response.status_code
        if status == 401:
            code = "unauthorized"
        elif status == 403:
            code = "access_denied"
        elif status == 404:
            code = "not_found"
        elif status == 429:
            code = "rate_limited"
        elif status >= 500:
            code = "upstream_unavailable"
        elif 300 <= status < 400:
            code = "unexpected_redirect"
        elif status != 200:
            code = "http_error"
        if code is not None:
            # Unexpected 2xx or redirects do not establish that no write occurred.
            raise MaxDeliveryError(
                code, status_code=status, delivery_uncertain=not 400 <= status < 500
            )

        try:
            return response.json()
        except ValueError:
            pass
        raise MaxDeliveryError("invalid_response", status_code=200, delivery_uncertain=True)

    async def send_text(
        self,
        user_id: int,
        text: str,
        buttons: Sequence[Sequence[CallbackButton]] | None = None,
    ) -> SentMessage:
        if type(user_id) is not int or not 0 < user_id < 2**63:
            raise MaxDeliveryError("invalid_request")
        if not _valid_text(text, maximum=MAX_TEXT_LENGTH):
            raise MaxDeliveryError("invalid_request")
        body: dict[str, object] = {"text": text}
        if buttons is not None:
            body["attachments"] = _keyboard(buttons)
        payload = await self._post(
            "/messages", params={"user_id": user_id, "disable_link_preview": True}, body=body
        )
        if isinstance(payload, dict) and isinstance(message := payload.get("message"), dict):
            message_body = message.get("body")
            if isinstance(message_body, dict):
                message_id = message_body.get("mid")
                if isinstance(message_id, str) and _valid_text(message_id):
                    return SentMessage(message_id=message_id)
        raise MaxDeliveryError("invalid_response", status_code=200, delivery_uncertain=True)

    async def answer_callback(self, callback_id: str) -> None:
        """Acknowledge without modifying a message or using undocumented fields."""
        if not _valid_text(callback_id):
            raise MaxDeliveryError("invalid_request")
        payload = await self._post("/answers", params={"callback_id": callback_id}, body={})
        if isinstance(payload, dict) and type(payload.get("success")) is bool:
            if payload["success"]:
                return
            raise MaxDeliveryError("callback_rejected", status_code=200)
        raise MaxDeliveryError("invalid_response", status_code=200, delivery_uncertain=True)
