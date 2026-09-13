"""Explicit public views; ORM secrets never enter response models."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    init_data: Annotated[SecretStr, Field(min_length=1, max_length=16384)]


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    max_user_id: int
    display_name: str
    is_active: bool
    is_owner: bool
    created_at: datetime


class LoginResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_at: datetime
    user: UserRead


class UsersPage(BaseModel):
    items: list[UserRead]
    limit: int
    offset: int
