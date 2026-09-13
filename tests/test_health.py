import logging
import logging.config
import socket
from unittest.mock import AsyncMock

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError
from uvicorn.config import LOGGING_CONFIG

from app.api.dependencies import get_db, get_redis
from app.core.config import Settings
from app.main import create_app


async def test_liveness_without_infrastructure(test_settings: Settings) -> None:
    app = create_app(test_settings)
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health/live")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
            assert len(response.headers["X-Request-ID"]) == 32


@pytest.mark.integration
async def test_readiness_with_real_services(client: AsyncClient) -> None:
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("failed_store", ["postgres", "redis"])
async def test_readiness_failure_is_safe(test_settings: Settings, failed_store: str) -> None:
    app = create_app(test_settings)
    database, redis = AsyncMock(), AsyncMock()
    if failed_store == "postgres":
        database.execute.side_effect = OperationalError("private-query", {}, Exception("secret"))
    else:
        redis.ping.side_effect = RedisConnectionError("redis://secret@private-host")
    app.dependency_overrides[get_db] = lambda: database
    app.dependency_overrides[get_redis] = lambda: redis
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


async def test_access_logging_never_records_raw_url_or_headers(
    test_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    app = create_app(test_settings)
    app_logger = logging.getLogger("app")
    app_logger.addHandler(caplog.handler)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/unknown-secret-path?initData=query-secret",
                headers={"Authorization": "Bearer auth-secret", "X-Request-ID": "header-secret"},
            )
    finally:
        app_logger.removeHandler(caplog.handler)
    assert response.status_code == 404
    for secret in ("unknown-secret-path", "query-secret", "auth-secret", "header-secret"):
        assert secret not in caplog.text
    assert "route=unmatched" in caplog.text


async def test_invalid_auth_body_does_not_echo_init_data(test_settings: Settings) -> None:
    app = create_app(test_settings)
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.dependency_overrides[get_redis] = lambda: AsyncMock()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/auth/max", json={"init_data": {"secret": "do-not-echo"}})
    assert response.status_code == 422
    assert "do-not-echo" not in response.text
    assert response.headers["Cache-Control"] == "no-store"


async def test_private_api_handles_real_database_connection_refusal(
    test_settings: Settings,
) -> None:
    # Reserve a local TCP port without listening: no arbitrary external address
    # or running service can be contacted by this failure-mode test.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        settings = test_settings.model_copy(
            update={
                "database_url": SecretStr(
                    f"postgresql+asyncpg://test:synthetic-secret@127.0.0.1:{port}/crm_test"
                )
            }
        )
        app = create_app(settings)
        async with LifespanManager(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get(
                    "/api/auth/me", headers={"Authorization": "Bearer " + "a" * 43}
                )
    assert response.status_code == 503
    assert response.json() == {"detail": "Service temporarily unavailable"}
    assert "synthetic-secret" not in response.text
    assert response.headers["Cache-Control"] == "no-store"


async def test_server_default_logging_keeps_application_access_events(
    test_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    logging.config.dictConfig(LOGGING_CONFIG)
    app = create_app(test_settings)
    app_logger = logging.getLogger("app")
    app_logger.addHandler(caplog.handler)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health/live")
    finally:
        app_logger.removeHandler(caplog.handler)
    assert logging.getLogger("app.access").isEnabledFor(logging.INFO)
    assert response.headers["X-Request-ID"] in caplog.text


@pytest.mark.parametrize(
    "exception", [ConnectionRefusedError("private-host"), TimeoutError("secret")]
)
async def test_dependency_connection_errors_are_safe(
    test_settings: Settings, exception: Exception
) -> None:
    app = create_app(test_settings)

    @app.get("/test-unavailable")
    async def fail() -> None:
        raise exception

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/test-unavailable")
    assert response.status_code == 503
    assert response.json() == {"detail": "Service temporarily unavailable"}
    assert response.headers["Cache-Control"] == "no-store"
