"""A deliberately read-only subset of the documented MAX API."""

from __future__ import annotations

import math
from types import TracebackType
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

MAX_API_BASE_URL = "https://platform-api2.max.ru"
Int64 = Annotated[int, Field(ge=-(2**63), le=2**63 - 1)]
Permission = Literal[
    "read_all_messages",
    "add_remove_members",
    "add_admins",
    "change_chat_info",
    "pin_message",
    "write",
    "can_call",
    "edit_link",
    "post_edit_delete_message",
    "edit_message",
    "delete_message",
    "edit",
    "delete",
    "view_stats",
]
ErrorCode = Literal[
    "invalid_configuration",
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
]


class MaxAPIError(Exception):
    """Safe diagnostics: never attach a request, response, or upstream text."""

    def __init__(self, code: ErrorCode, *, status_code: int | None = None) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(f"MAX API: {code}")


class _ResponseProjection(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="ignore", hide_input_in_errors=True)


class BotIdentity(_ResponseProjection):
    """Only the documented identity fields needed by the probe are retained."""

    user_id: Int64
    is_bot: bool


class BotMembership(BotIdentity):
    is_admin: bool
    is_owner: bool
    # MAX omits this field for ordinary members and permits null.
    permissions: list[Permission] | None = None

    @property
    def read_all_messages(self) -> bool | None:
        if self.permissions is None:
            return None
        return "read_all_messages" in self.permissions


class _SubscriptionsResponse(_ResponseProjection):
    # The public method documents Subscription[]; inner fields are not consumed.
    subscriptions: list[dict[str, object]] = Field(repr=False, exclude=True)


def _parse_response[T: _ResponseProjection](model: type[T], payload: object) -> T:
    try:
        return model.model_validate(payload)
    except ValidationError:
        pass
    # Raise outside the except block so no raw payload survives in __context__.
    raise MaxAPIError("invalid_response")


def _validate_base_url(base_url: str) -> None:
    try:
        parsed = urlsplit(base_url)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname == "platform-api2.max.ru"
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
            and not any(character.isspace() for character in base_url)
        )
    except ValueError:
        valid = False
    if not valid:
        raise MaxAPIError("invalid_configuration")


class MaxReadOnlyClient:
    """Own a TLS-verified client; inject MockTransport for offline tests."""

    def __init__(
        self,
        *,
        token: SecretStr,
        base_url: str = MAX_API_BASE_URL,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        _validate_base_url(base_url)
        if (
            isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise MaxAPIError("invalid_configuration")
        if not isinstance(token, SecretStr):
            raise MaxAPIError("invalid_configuration")
        value = token.get_secret_value()
        if not value or any(ord(character) < 33 or ord(character) > 126 for character in value):
            raise MaxAPIError("invalid_configuration")
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

    async def _get(self, path: str) -> object:
        failure: ErrorCode | None = None
        response: httpx.Response | None = None
        try:
            response = await self._client.get(
                path,
                headers={"Authorization": self._token.get_secret_value()},
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            failure = "timeout"
        except httpx.RequestError:
            failure = "network_error"
        if failure is not None:
            raise MaxAPIError(failure)
        assert response is not None

        code: ErrorCode | None = None
        if response.status_code == 401:
            code = "unauthorized"
        elif response.status_code == 403:
            code = "access_denied"
        elif response.status_code == 404:
            code = "not_found"
        elif response.status_code == 429:
            code = "rate_limited"
        elif response.status_code >= 500:
            code = "upstream_unavailable"
        elif 300 <= response.status_code < 400:
            code = "unexpected_redirect"
        elif response.status_code != 200:
            code = "http_error"
        if code:
            raise MaxAPIError(code, status_code=response.status_code)

        try:
            return response.json()
        except ValueError:
            pass
        raise MaxAPIError("invalid_response")

    async def get_me(self) -> BotIdentity:
        payload = await self._get("/me")
        bot = _parse_response(BotIdentity, payload)
        if not bot.is_bot:
            raise MaxAPIError("invalid_response")
        return bot

    async def has_subscriptions(self) -> bool:
        payload = await self._get("/subscriptions")
        response = _parse_response(_SubscriptionsResponse, payload)
        return bool(response.subscriptions)

    async def get_membership(self, chat_id: int) -> BotMembership:
        if type(chat_id) is not int or not -(2**63) <= chat_id < 2**63:
            raise MaxAPIError("invalid_configuration")
        payload = await self._get(f"/chats/{chat_id}/members/me")
        membership = _parse_response(BotMembership, payload)
        if not membership.is_bot:
            raise MaxAPIError("invalid_response")
        return membership
