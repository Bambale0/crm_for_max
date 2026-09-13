"""Async Alembic environment. Connection secrets never pass through INI interpolation."""

import asyncio

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.models import identity  # noqa: F401 -- register mapped tables for autogenerate
from app.models.base import Base


def migrate_connection(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def migrate_online(url: str) -> None:
    engine = create_async_engine(
        url,
        poolclass=pool.NullPool,
        hide_parameters=True,
        connect_args={"timeout": 5, "command_timeout": 30},
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(migrate_connection)
    finally:
        await engine.dispose()


def run() -> None:
    url = Settings().database_url.get_secret_value()
    if context.is_offline_mode():
        context.configure(
            url=url,
            target_metadata=Base.metadata,
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
        )
        with context.begin_transaction():
            context.run_migrations()
    else:
        asyncio.run(migrate_online(url))


try:
    run()
except Exception as error:
    # Drivers/configuration errors can contain connection strings or SQL values.
    raise SystemExit(
        f"Migration failed ({type(error).__name__}); check database access and configuration."
    ) from None
