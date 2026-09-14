from collections.abc import AsyncIterator
from typing import cast

from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import Database


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_redis(request: Request) -> Redis:
    return cast(Redis, request.app.state.redis)


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    database = cast(Database, request.app.state.database)
    async with database.session_factory() as session:
        yield session
