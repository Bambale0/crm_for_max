"""Offline synthetic fixtures follow the linked MAX contracts; no live tokens."""

from __future__ import annotations

import json
import logging
import traceback
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.integrations.max import probe
from app.integrations.max.client import MAX_API_BASE_URL, MaxAPIError, MaxReadOnlyClient

TOKEN = "synthetic-max-token-do-not-use"
BOT = {
    "user_id": 123,
    "first_name": "Synthetic bot",
    "username": "synthetic_bot",
    "is_bot": True,
    "last_activity_time": 0,
}
MEMBERSHIP = {
    **BOT,
    "last_access_time": 0,
    "is_owner": False,
    "is_admin": True,
    "join_time": 0,
    "permissions": ["read_all_messages", "write"],
}


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> MaxReadOnlyClient:
    return MaxReadOnlyClient(
        token=SecretStr(TOKEN), transport=httpx.MockTransport(handler), timeout_seconds=3
    )


@pytest.mark.asyncio
async def test_probe_uses_only_documented_get_requests_and_safe_output(
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx.Request] = []
    payloads = {
        "/me": BOT,
        "/subscriptions": {"subscriptions": [{"url": f"https://example.invalid/{TOKEN}"}]},
        "/chats/-456/members/me": MEMBERSHIP,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.scheme == "https"
        assert request.url.host == "platform-api2.max.ru"
        assert request.url.query == b""
        assert request.headers["Authorization"] == TOKEN
        assert request.extensions["timeout"]["read"] == 3
        return httpx.Response(200, json=payloads[request.url.path])

    caplog.set_level(logging.DEBUG)
    async with make_client(handler) as client:
        report = await probe.probe_bot(client, chat_id=-456)
        assert TOKEN not in repr(client)
    assert [request.url.path for request in requests] == list(payloads)
    assert report.bot_id == 123
    assert report.has_subscriptions is True
    assert report.membership_checked is True
    assert report.read_all_messages is True
    assert report.end_to_end_verified is False
    assert TOKEN not in repr(report) + caplog.text
    assert "example.invalid" not in repr(report)


@pytest.mark.asyncio
async def test_empty_subscriptions_and_skipped_membership_are_explicit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/me":
            return httpx.Response(200, json=BOT)
        assert request.url.path == "/subscriptions"
        return httpx.Response(200, json={"subscriptions": []})

    async with make_client(handler) as client:
        report = await probe.probe_bot(client)
    assert report.has_subscriptions is False
    assert report.membership_checked is False
    assert report.is_admin is None
    assert report.permissions is None
    assert report.read_all_messages is None


@pytest.mark.asyncio
@pytest.mark.parametrize("permission_fields", [{}, {"permissions": None}, {"permissions": []}])
async def test_missing_permissions_are_not_mistaken_for_verified_read_access(
    permission_fields: dict[str, object],
) -> None:
    payload = {key: value for key, value in MEMBERSHIP.items() if key != "permissions"}
    payload.update(permission_fields)
    async with make_client(lambda _: httpx.Response(200, json=payload)) as client:
        membership = await client.get_membership(-456)
    expected = False if permission_fields.get("permissions") == [] else None
    assert membership.read_all_messages is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "unauthorized"),
        (403, "access_denied"),
        (404, "not_found"),
        (429, "rate_limited"),
        (500, "upstream_unavailable"),
        (503, "upstream_unavailable"),
        (302, "unexpected_redirect"),
        (307, "unexpected_redirect"),
        (400, "http_error"),
        (201, "http_error"),
    ],
)
async def test_status_errors_do_not_expose_provider_payloads_or_follow_redirects(
    status: int, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status,
            text=f"sensitive upstream details {TOKEN}",
            headers={"Location": f"https://example.invalid/{TOKEN}", "Retry-After": "10"},
        )

    caplog.set_level(logging.DEBUG)
    async with make_client(handler) as client:
        with pytest.raises(MaxAPIError) as caught:
            await client.get_me()
    assert caught.value.code == code
    assert caught.value.status_code == status
    assert len(requests) == 1
    rendered = "".join(traceback.format_exception(caught.value)) + caplog.text
    assert TOKEN not in rendered
    assert "sensitive upstream details" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "code"),
    [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "network_error")],
)
async def test_network_and_tls_failures_are_sanitized(
    error_type: type[httpx.RequestError], code: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error_type(f"TLS or timeout details {TOKEN}", request=request)

    async with make_client(handler) as client:
        with pytest.raises(MaxAPIError) as caught:
            await client.get_me()
    assert caught.value.code == code
    assert caught.value.__context__ is None
    assert TOKEN not in "".join(traceback.format_exception(caught.value))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [None, [], {}, {**BOT, "user_id": TOKEN}, {**BOT, "user_id": True}, {**BOT, "is_bot": False}],
)
async def test_malformed_identity_is_rejected_without_payload_echo(payload: object) -> None:
    async with make_client(lambda _: httpx.Response(200, json=payload)) as client:
        with pytest.raises(MaxAPIError, match="invalid_response") as caught:
            await client.get_me()
    assert caught.value.__context__ is None
    assert TOKEN not in "".join(traceback.format_exception(caught.value))


@pytest.mark.asyncio
async def test_invalid_json_is_sanitized() -> None:
    async with make_client(lambda _: httpx.Response(200, text=TOKEN)) as client:
        with pytest.raises(MaxAPIError, match="invalid_response") as caught:
            await client.get_me()
    assert caught.value.__context__ is None
    assert TOKEN not in "".join(traceback.format_exception(caught.value))


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"subscriptions": None}, {"subscriptions": [TOKEN]}])
async def test_invalid_subscription_container_is_rejected(payload: object) -> None:
    async with make_client(lambda _: httpx.Response(200, json=payload)) as client:
        with pytest.raises(MaxAPIError, match="invalid_response"):
            await client.has_subscriptions()


@pytest.mark.asyncio
async def test_unknown_permission_value_is_not_printed() -> None:
    payload = {**MEMBERSHIP, "permissions": [TOKEN]}
    async with make_client(lambda _: httpx.Response(200, json=payload)) as client:
        with pytest.raises(MaxAPIError, match="invalid_response") as caught:
            await client.get_membership(-456)
    assert TOKEN not in "".join(traceback.format_exception(caught.value))


@pytest.mark.asyncio
async def test_membership_must_match_authenticated_bot() -> None:
    payloads = {
        "/me": BOT,
        "/subscriptions": {"subscriptions": []},
        "/chats/-456/members/me": {**MEMBERSHIP, "user_id": 999},
    }
    async with make_client(lambda r: httpx.Response(200, json=payloads[r.url.path])) as client:
        with pytest.raises(MaxAPIError, match="invalid_response"):
            await probe.probe_bot(client, chat_id=-456)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://platform-api2.max.ru",
        "https://example.invalid",
        "https://platform-api2.max.ru.example.invalid",
        "https://platform-api2.max.ru@evil.invalid",
        f"https://{TOKEN}@platform-api2.max.ru",
        f"{MAX_API_BASE_URL}/?access_token={TOKEN}",
        f"{MAX_API_BASE_URL}/#{TOKEN}",
        f"{MAX_API_BASE_URL}/extra",
        f"{MAX_API_BASE_URL}:8443",
        f"{MAX_API_BASE_URL}:invalid",
        f" {MAX_API_BASE_URL}",
    ],
)
def test_unexpected_base_url_is_rejected_before_sending_auth(base_url: str) -> None:
    with pytest.raises(MaxAPIError, match="invalid_configuration") as caught:
        MaxReadOnlyClient(token=SecretStr(TOKEN), base_url=base_url)
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
def test_invalid_timeout_is_rejected(timeout: float) -> None:
    with pytest.raises(MaxAPIError, match="invalid_configuration"):
        MaxReadOnlyClient(token=SecretStr(TOKEN), timeout_seconds=timeout)


@pytest.mark.parametrize("token", ["", " ", "bad\r\nheader", "не-ascii"])
def test_invalid_token_header_is_rejected(token: str) -> None:
    with pytest.raises(MaxAPIError, match="invalid_configuration"):
        MaxReadOnlyClient(token=SecretStr(token))


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_id", [True, 2**63, -(2**63) - 1, "../me"])
async def test_invalid_chat_id_cannot_change_request_path(chat_id: object) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid chat ID must be rejected before an HTTP request")

    async with make_client(handler) as client:
        with pytest.raises(MaxAPIError, match="invalid_configuration"):
            await client.get_membership(chat_id)  # type: ignore[arg-type]


def test_cli_missing_token_does_not_call_network(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(_env_file=None, max_observer_token=None, max_staff_token=None)
    monkeypatch.setattr(probe, "Settings", lambda: settings)
    monkeypatch.setattr(probe, "MaxReadOnlyClient", lambda **_: pytest.fail("No token"))
    assert probe.main(["--bot", "observer"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error": "missing_token"}


@pytest.mark.parametrize("role", ["observer", "staff"])
def test_cli_selects_token_and_prints_only_safe_report(
    role: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(
        _env_file=None,
        max_observer_token=SecretStr("synthetic-observer-token"),
        max_staff_token=SecretStr("synthetic-staff-token"),
    )
    monkeypatch.setattr(probe, "Settings", lambda: settings)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"synthetic-{role}-token"
        payload = BOT if request.url.path == "/me" else {"subscriptions": []}
        return httpx.Response(200, json=payload)

    def factory(**kwargs: object) -> MaxReadOnlyClient:
        return MaxReadOnlyClient(**kwargs, transport=httpx.MockTransport(handler))  # type: ignore[arg-type]

    monkeypatch.setattr(probe, "MaxReadOnlyClient", factory)
    assert probe.main(["--bot", role]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["bot_id"] == 123
    assert "token" not in captured.out
    assert "Synthetic bot" not in captured.out


def test_cli_api_failure_has_nonzero_exit_without_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(_env_file=None, max_observer_token=SecretStr(TOKEN))
    monkeypatch.setattr(probe, "Settings", lambda: settings)

    def factory(**_: object) -> MaxReadOnlyClient:
        return make_client(lambda _: httpx.Response(401, text=TOKEN))

    monkeypatch.setattr(probe, "MaxReadOnlyClient", factory)
    assert probe.main(["--bot", "observer"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error": "unauthorized"}
    assert TOKEN not in captured.err
