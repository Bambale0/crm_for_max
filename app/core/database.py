"""One connection pool per application instance."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


class Database:
    def __init__(self, url: str) -> None:
        self.engine = create_async_engine(
            url,
            pool_pre_ping=True,
            pool_timeout=5,
            hide_parameters=True,
            connect_args={"timeout": 5, "command_timeout": 10},
        )
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def close(self) -> None:
        await self.engine.dispose()
