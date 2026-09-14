"""Integration tests use disposable databases and roll back every SQL transaction."""

import hashlib
import hmac
import json
import os
import time
from collections.abc import AsyncIterator
from urllib.parse import quote, urlencode
from uuid import uuid4

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.dependencies import get_db, get_deepseek_classifier, get_redis
from app.core.config import Settings
from app.integrations.deepseek.client import DeepSeekResult
from app.main import create_app


class SyntheticDeepSeekClassifier:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def classify(self, text: str) -> DeepSeekResult:
        self.calls.append(text)
        value = text.casefold().replace("ё", "е")
        resolved = any(
            marker in value
            for marker in ("починили", "уже работает", "все нормально", "всё нормально")
        )
        contrast = any(marker in f" {value} " for marker in (" но ", " однако ", " при этом "))
        urgent = any(
            marker in value
            for marker in ("пахнет газом", "запах газа", "дым", "искрит", "затапливает")
        )
        problem = urgent or any(
            marker in value
            for marker in (
                "теч",
                "не работает",
                "нет воды",
                "воды нет",
                "нет света",
                "света нет",
                "мусор не вывоз",
                "канализац",
            )
        )
        if resolved and not contrast and not urgent:
            problem = False
        return DeepSeekResult(
            is_problem=problem,
            problem=text if problem else "",
            confidence=0.97,
            severity="urgent" if urgent else "normal",
        )


@pytest.fixture
def deepseek_classifier() -> SyntheticDeepSeekClassifier:
    return SyntheticDeepSeekClassifier()


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_url=SecretStr(
            os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://test@127.0.0.1:5432/crm_test")
        ),
        redis_url=SecretStr(os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6379/1")),
        max_staff_token=SecretStr("synthetic-max-staff-token"),
        max_observer_token=None,
        max_owner_ids=(101,),
        max_operator_ids=(404,),
        max_employee_ids=(202,),
    )


@pytest.fixture
async def db_session(test_settings: Settings) -> AsyncIterator[AsyncSession]:
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL must point to a disposable migrated PostgreSQL database")
    engine = create_async_engine(
        test_settings.database_url.get_secret_value(), hide_parameters=True
    )
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                async with AsyncSession(
                    bind=connection,
                    expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                ) as session:
                    yield session
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
async def redis_client(test_settings: Settings) -> AsyncIterator[Redis]:
    if not os.environ.get("TEST_REDIS_URL"):
        pytest.skip("TEST_REDIS_URL must point to a disposable Redis database")
    client = Redis.from_url(test_settings.redis_url.get_secret_value(), decode_responses=True)
    # Never flush a datastore: only remove auth keys newly created by this test.
    existing_keys = {key async for key in client.scan_iter(match="auth:*")}
    try:
        yield client
    finally:
        new_keys = {key async for key in client.scan_iter(match="auth:*")} - existing_keys
        if new_keys:
            await client.delete(*new_keys)
        await client.aclose()


@pytest.fixture
def app(
    test_settings: Settings,
    db_session: AsyncSession,
    redis_client: Redis,
    deepseek_classifier: SyntheticDeepSeekClassifier,
) -> FastAPI:
    instance = create_app(test_settings)

    async def test_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    instance.dependency_overrides[get_db] = test_db
    instance.dependency_overrides[get_redis] = lambda: redis_client
    instance.dependency_overrides[get_deepseek_classifier] = lambda: deepseek_classifier
    return instance


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            yield http


async def _max_headers(client: AsyncClient, settings: Settings, max_user_id: int) -> dict[str, str]:
    assert settings.max_staff_token is not None
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": str(uuid4()),
        "user": json.dumps({"id": max_user_id, "first_name": "Synthetic employee"}),
    }
    secret = hmac.digest(
        b"WebAppData", settings.max_staff_token.get_secret_value().encode(), "sha256"
    )
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    response = await client.post(
        "/api/auth/max", json={"init_data": urlencode(fields, quote_via=quote)}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}


@pytest.fixture
async def owner_headers(client: AsyncClient, test_settings: Settings) -> dict[str, str]:
    return await _max_headers(client, test_settings, 101)


@pytest.fixture
async def employee_headers(client: AsyncClient, test_settings: Settings) -> dict[str, str]:
    return await _max_headers(client, test_settings, 202)
