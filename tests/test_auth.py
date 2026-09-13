"""Authentication and access checks against actual PostgreSQL and Redis."""

import asyncio
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, quote, urlencode
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_redis
from app.auth.rate_limit import (
    InitDataReplayed,
    LoginRateLimited,
    check_login_budget,
    consume_init_data,
)
from app.auth.security import token_digest
from app.core.config import Settings
from app.models.identity import AuditLog, AuthSession, User

pytestmark = pytest.mark.integration


def _launch(settings: Settings, max_user_id: int = 101) -> str:
    assert settings.max_staff_token is not None
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": str(uuid4()),
        "user": json.dumps({"id": max_user_id, "first_name": "Тест", "last_name": "User"}),
    }
    data = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    key = hmac.digest(b"WebAppData", settings.max_staff_token.get_secret_value().encode(), "sha256")
    signature = hmac.new(key, data.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": signature}, quote_via=quote)


async def _login(client: AsyncClient, settings: Settings, max_user_id: int = 101) -> Response:
    return await client.post("/api/auth/max", json={"init_data": _launch(settings, max_user_id)})


def _headers(response: Response) -> dict[str, str]:
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}


async def test_owner_login_me_logout_and_safe_audit(
    client: AsyncClient, test_settings: Settings, db_session: AsyncSession
) -> None:
    logged_in = await _login(client, test_settings)
    headers = _headers(logged_in)
    token = logged_in.json()["access_token"]
    assert logged_in.headers["Cache-Control"] == "no-store"
    assert logged_in.json()["user"]["is_owner"] is True
    current = await client.get("/api/auth/me", headers=headers)
    assert current.status_code == 200
    assert current.json() == logged_in.json()["user"]
    assert set(current.json()) == {
        "id",
        "max_user_id",
        "display_name",
        "is_active",
        "is_owner",
        "created_at",
    }
    saved_session = await db_session.scalar(select(AuthSession))
    assert saved_session is not None
    assert saved_session.token_hash == token_digest(token)
    assert saved_session.token_hash != token
    logged_out = await client.post("/api/auth/logout", headers=headers)
    assert logged_out.status_code == 204
    assert logged_out.content == b""
    rejected = await client.get("/api/auth/me", headers=headers)
    assert rejected.status_code == 401
    events = list(await db_session.scalars(select(AuditLog)))
    assert sorted(event.action for event in events) == ["auth.login", "auth.logout", "user.created"]
    assert all(event.actor_id == saved_session.user_id for event in events)
    assert set(AuditLog.__table__.columns.keys()) == {
        "id",
        "actor_id",
        "subject_id",
        "action",
        "created_at",
    }


async def test_allowlisted_employee_cannot_access_owner_list(
    client: AsyncClient, test_settings: Settings
) -> None:
    headers = _headers(await _login(client, test_settings, 202))
    assert (await client.get("/api/auth/me", headers=headers)).json()["is_owner"] is False
    denied = await client.get("/api/admin/users", headers=headers)
    assert denied.status_code == 403


async def test_owner_list_is_bounded_and_does_not_expose_sessions(
    client: AsyncClient, test_settings: Settings
) -> None:
    owner = await _login(client, test_settings)
    employee = await _login(client, test_settings, 202)
    assert employee.status_code == 200
    page = await client.get("/api/admin/users?limit=1&offset=1", headers=_headers(owner))
    assert page.status_code == 200
    assert page.json()["limit"] == 1
    assert page.json()["offset"] == 1
    assert len(page.json()["items"]) == 1
    assert "token" not in page.text
    empty = await client.get("/api/admin/users?offset=2", headers=_headers(owner))
    assert empty.json()["items"] == []
    for query in ("limit=101", "limit=0", "offset=-1", "offset=10001"):
        assert (
            await client.get("/api/admin/users?" + query, headers=_headers(owner))
        ).status_code == 422


@pytest.mark.parametrize("path", ["/api/auth/me", "/api/admin/users"])
async def test_private_routes_require_bearer_authentication(client: AsyncClient, path: str) -> None:
    response = await client.get(path)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("state", ["expired", "revoked", "inactive"])
async def test_invalidated_sessions_are_rejected_on_next_request(
    client: AsyncClient, test_settings: Settings, db_session: AsyncSession, state: str
) -> None:
    headers = _headers(await _login(client, test_settings))
    session = await db_session.scalar(select(AuthSession))
    user = await db_session.scalar(select(User))
    assert session is not None and user is not None
    if state == "expired":
        session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif state == "revoked":
        session.revoked_at = datetime.now(UTC)
    else:
        user.is_active = False
    await db_session.commit()
    assert (await client.get("/api/auth/me", headers=headers)).status_code == 401


async def test_environment_removal_and_owner_demotion_affect_existing_session(
    client: AsyncClient, test_settings: Settings
) -> None:
    headers = _headers(await _login(client, test_settings))
    test_settings.max_owner_ids = ()
    test_settings.max_employee_ids = (101, 202)
    assert (await client.get("/api/admin/users", headers=headers)).status_code == 403
    assert (await client.get("/api/auth/me", headers=headers)).json()["is_owner"] is False
    test_settings.max_employee_ids = (202,)
    assert (await client.get("/api/auth/me", headers=headers)).status_code == 401


async def test_unlisted_signed_identity_creates_no_account(
    client: AsyncClient, test_settings: Settings, db_session: AsyncSession
) -> None:
    denied = await _login(client, test_settings, 303)
    assert denied.status_code == 401
    assert await db_session.scalar(select(func.count()).select_from(User)) == 0
    assert await db_session.scalar(select(func.count()).select_from(AuthSession)) == 0


async def test_disabled_identity_cannot_log_in_again(
    client: AsyncClient, test_settings: Settings, db_session: AsyncSession
) -> None:
    assert (await _login(client, test_settings)).status_code == 200
    user = await db_session.scalar(select(User))
    assert user is not None
    user.is_active = False
    await db_session.commit()
    assert (await _login(client, test_settings)).status_code == 401
    assert await db_session.scalar(select(func.count()).select_from(AuthSession)) == 1


async def test_repeat_launch_uses_one_identity_and_replay_is_rejected(
    client: AsyncClient, test_settings: Settings, db_session: AsyncSession
) -> None:
    launch = _launch(test_settings)
    first = await client.post("/api/auth/max", json={"init_data": launch})
    assert first.status_code == 200
    replay = await client.post("/api/auth/max", json={"init_data": launch})
    assert replay.status_code == 401
    assert (await _login(client, test_settings)).status_code == 200
    assert await db_session.scalar(select(func.count()).select_from(User)) == 1
    assert await db_session.scalar(select(func.count()).select_from(AuthSession)) == 2
    assert (
        await db_session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "user.created")
        )
        == 1
    )


async def test_database_rejects_replay_after_redis_key_loss(
    client: AsyncClient, test_settings: Settings, redis_client: Redis, db_session: AsyncSession
) -> None:
    launch = _launch(test_settings)
    assert (await client.post("/api/auth/max", json={"init_data": launch})).status_code == 200
    signature = parse_qs(launch)["hash"][0]
    # Simulate Redis restart/eviction using only this test's own key.
    await redis_client.delete("auth:launch:" + token_digest(signature))
    assert (await client.post("/api/auth/max", json={"init_data": launch})).status_code == 401
    assert await db_session.scalar(select(func.count()).select_from(AuthSession)) == 1
    assert (
        await db_session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "auth.login")
        )
        == 1
    )


@pytest.mark.parametrize("disabled", ["token", "allowlist"])
async def test_unconfigured_max_login_is_unavailable(
    client: AsyncClient, test_settings: Settings, disabled: str
) -> None:
    launch = _launch(test_settings)
    if disabled == "token":
        test_settings.max_staff_token = None
    else:
        test_settings.max_owner_ids = ()
        test_settings.max_employee_ids = ()
    response = await client.post("/api/auth/max", json={"init_data": launch})
    assert response.status_code == 503


async def test_unsigned_login_and_oversized_body_are_redacted(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    sensitive = "synthetic-sensitive-init-data"
    invalid = await client.post("/api/auth/max", json={"init_data": sensitive})
    oversized = await client.post("/api/auth/max", json={"init_data": sensitive * 1000})
    assert invalid.status_code == 401
    assert oversized.status_code == 422
    assert sensitive not in invalid.text + oversized.text + caplog.text
    assert (await client.post("/api/auth/login", json={})).status_code == 404


async def test_redis_failure_is_closed_and_redacted(
    client: AsyncClient,
    app: FastAPI,
    test_settings: Settings,
    db_session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "synthetic-secret-redis-password"
    broken = AsyncMock(spec=Redis)
    broken.eval.side_effect = RedisConnectionError("redis://user:" + secret + "@localhost")
    app.dependency_overrides[get_redis] = lambda: broken
    response = await _login(client, test_settings)
    assert response.status_code == 503
    assert secret not in response.text + caplog.text
    assert await db_session.scalar(select(func.count()).select_from(AuthSession)) == 0


async def test_http_throttle_supplies_retry_after(
    client: AsyncClient, test_settings: Settings
) -> None:
    test_settings.login_rate_limit = 1
    first = await client.post("/api/auth/max", json={"init_data": "invalid"})
    second = await client.post("/api/auth/max", json={"init_data": "invalid"})
    assert first.status_code == 401
    assert second.status_code == 429
    assert 1 <= int(second.headers["Retry-After"]) <= test_settings.login_rate_window_seconds


async def test_redis_rate_budget_is_atomic_with_bounded_ttl(redis_client: Redis) -> None:
    identifier = str(uuid4())
    results = await asyncio.gather(
        *(
            check_login_budget(
                redis_client, scope="test", identifier=identifier, limit=3, window_seconds=60
            )
            for _ in range(10)
        ),
        return_exceptions=True,
    )
    assert results.count(None) == 3
    assert sum(isinstance(result, LoginRateLimited) for result in results) == 7
    assert 1 <= await redis_client.ttl("auth:login:test:" + token_digest(identifier)) <= 60


async def test_init_data_consumption_is_atomic(redis_client: Redis) -> None:
    signature = str(uuid4())
    results = await asyncio.gather(
        consume_init_data(redis_client, signature=signature, ttl_seconds=331),
        consume_init_data(redis_client, signature=signature, ttl_seconds=331),
        return_exceptions=True,
    )
    assert results.count(None) == 1
    assert sum(isinstance(result, InitDataReplayed) for result in results) == 1
    assert 1 <= await redis_client.ttl("auth:launch:" + token_digest(signature)) <= 331
