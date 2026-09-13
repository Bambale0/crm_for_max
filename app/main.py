"""Application factory with explicit dependency lifetimes."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from app.api.health import router as health_router
from app.auth.routes import router as auth_router
from app.core.config import Settings
from app.core.database import Database
from app.core.logging import configure_logging
from app.core.request_logging import RequestLoggingMiddleware

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_logging()
    configuration = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(configuration.database_url.get_secret_value())
        redis = Redis.from_url(
            configuration.redis_url.get_secret_value(),
            socket_connect_timeout=3,
            socket_timeout=3,
            decode_responses=True,
        )
        app.state.database = database
        app.state.redis = redis
        try:
            yield
        finally:
            try:
                await redis.aclose()
            finally:
                await database.close()

    app = FastAPI(
        title="Единая цифровая диспетчерская УК",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if configuration.environment != "production" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if configuration.environment != "production" else None,
    )
    app.state.settings = configuration
    app.add_middleware(RequestLoggingMiddleware)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI defaults include raw input: a rejected initData is still secret.
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {key: error[key] for key in ("loc", "msg", "type")} for error in exc.errors()
                ]
            },
            headers={"Cache-Control": "no-store"},
        )

    async def unavailable(request: Request, exc: Exception) -> JSONResponse:
        logger.warning("dependency_unavailable request_id=%s", request.state.request_id)
        return JSONResponse(
            status_code=503,
            content={"detail": "Service temporarily unavailable"},
            headers={"Cache-Control": "no-store"},
        )

    app.add_exception_handler(SQLAlchemyError, unavailable)
    app.add_exception_handler(RedisError, unavailable)
    # asyncpg may surface connection refusal and timeout before SQLAlchemy can
    # wrap the error. Do not expose infrastructure exceptions through the API.
    app.add_exception_handler(OSError, unavailable)
    app.add_exception_handler(TimeoutError, unavailable)
    app.include_router(health_router)
    app.include_router(auth_router)
    return app
