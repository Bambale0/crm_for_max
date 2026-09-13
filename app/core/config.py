"""Typed environment configuration; all secret values have redacted reprs."""

import re
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        populate_by_name=True,
        hide_input_in_errors=True,
    )

    environment: Literal["development", "test", "production"] = Field(
        default="development", validation_alias="APP_ENV"
    )
    database_url: SecretStr = SecretStr("postgresql+asyncpg://crm@127.0.0.1:5432/crm")
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
    max_staff_token: SecretStr | None = None
    max_observer_token: SecretStr | None = None
    max_staff_webhook_secret: SecretStr | None = None
    max_observer_webhook_secret: SecretStr | None = None
    max_bot_organization_id: UUID | None = None
    max_owner_ids: Annotated[tuple[int, ...], NoDecode] = ()
    max_employee_ids: Annotated[tuple[int, ...], NoDecode] = ()
    max_api_base_url: str = "https://platform-api2.max.ru"
    max_timeout_seconds: float = Field(default=10, gt=0, le=60)
    max_init_data_ttl_seconds: int = Field(default=300, ge=30, le=900)
    max_init_data_future_skew_seconds: int = Field(default=30, ge=0, le=60)
    session_ttl_seconds: int = Field(default=28800, ge=60, le=86400)
    login_rate_limit: int = Field(default=10, ge=1, le=100)
    login_rate_window_seconds: int = Field(default=60, ge=1, le=3600)

    @field_validator("max_staff_webhook_secret", "max_observer_webhook_secret")
    @classmethod
    def check_webhook_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not re.fullmatch(
            r"[A-Za-z0-9_-]{32,256}", value.get_secret_value()
        ):
            raise ValueError("Webhook secret must contain 32-256 ASCII letters, digits, _ or -")
        return value

    @model_validator(mode="after")
    def distinct_webhook_secrets(self) -> Self:
        if (
            self.max_staff_webhook_secret is not None
            and self.max_observer_webhook_secret is not None
            and self.max_staff_webhook_secret == self.max_observer_webhook_secret
        ):
            raise ValueError("Staff and observer webhook secrets must differ")
        return self

    @field_validator("max_owner_ids", "max_employee_ids", mode="before")
    @classmethod
    def parse_max_ids(cls, value: object) -> tuple[int, ...]:
        if isinstance(value, str):
            values: object = value.split(",") if value.strip() else ()
        else:
            values = value
        if not isinstance(values, (tuple, list)):
            raise ValueError("MAX IDs must be a comma-separated list of positive integers")
        result: list[int] = []
        for item in values:
            if isinstance(item, str) and item.strip().isascii() and item.strip().isdecimal():
                item = int(item.strip())
            if type(item) is not int or not 0 < item < 2**63:
                raise ValueError("MAX IDs must be positive signed 64-bit integers")
            result.append(item)
        return tuple(dict.fromkeys(result))

    @field_validator("database_url")
    @classmethod
    def check_database_url(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
            if url.drivername != "postgresql+asyncpg" or not url.host or not url.database:
                raise ValueError
        except (ArgumentError, ValueError):
            raise ValueError("DATABASE_URL must be a PostgreSQL asyncpg connection URL") from None
        return value

    @field_validator("redis_url")
    @classmethod
    def check_redis_url(cls, value: SecretStr) -> SecretStr:
        from urllib.parse import urlsplit

        try:
            url = urlsplit(value.get_secret_value())
            if url.scheme not in ("redis", "rediss") or not url.hostname:
                raise ValueError
            _ = url.port
        except ValueError:
            raise ValueError("REDIS_URL must be a Redis connection URL") from None
        return value
