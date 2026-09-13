"""Integration tests use disposable databases and roll back every SQL transaction."""

import os
from collections.abc import AsyncIterator

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.dependencies import get_db, get_redis
from app.core.config import Settings
from app.main import create_app


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
def app(test_settings: Settings, db_session: AsyncSession, redis_client: Redis) -> FastAPI:
    instance = create_app(test_settings)

    async def test_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    instance.dependency_overrides[get_db] = test_db
    instance.dependency_overrides[get_redis] = lambda: redis_client
    return instance


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            yield http
