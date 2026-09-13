"""Validated catalog inputs and explicit public responses."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.crm.access import SUPPORTED_PERMISSIONS

Name = Annotated[str, Field(min_length=1, max_length=200)]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ViewModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page[T](BaseModel):
    items: list[T]
    limit: int
    offset: int


class OrganizationCreate(InputModel):
    name: Name


class OrganizationRead(ViewModel):
    id: UUID
    name: str
    created_at: datetime


class NamedCreate(InputModel):
    organization_id: UUID
    name: Name


class NamedRead(ViewModel):
    id: UUID
    organization_id: UUID
    name: str


class AreaCreate(NamedCreate):
    district_id: UUID


class AreaRead(NamedRead):
    district_id: UUID


class HouseCreate(InputModel):
    organization_id: UUID
    area_id: UUID
    address: Annotated[str, Field(min_length=1, max_length=500)]


class HouseRead(ViewModel):
    id: UUID
    organization_id: UUID
    area_id: UUID
    address: str


class EntranceCreate(InputModel):
    organization_id: UUID
    house_id: UUID
    number: Annotated[str, Field(min_length=1, max_length=20)]


class EntranceRead(ViewModel):
    id: UUID
    organization_id: UUID
    house_id: UUID
    number: str


def checked_permissions(value: list[str]) -> list[str]:
    if not set(value) <= SUPPORTED_PERMISSIONS:
        raise ValueError("Unsupported CRM permission")
    return sorted(set(value))


class RoleCreate(NamedCreate):
    permissions: list[str] = Field(default_factory=list, max_length=50)

    _permissions = field_validator("permissions")(checked_permissions)


class RoleRead(NamedRead):
    permissions: list[str]


class RolePatch(InputModel):
    name: Name | None = None
    permissions: list[str] | None = Field(default=None, max_length=50)

    @model_validator(mode="after")
    def check_fields(self) -> "RolePatch":
        if not self.model_fields_set or any(
            getattr(self, key) is None for key in self.model_fields_set
        ):
            raise ValueError("Provide at least one non-null field")
        if self.permissions is not None:
            self.permissions = checked_permissions(self.permissions)
        return self


class EmployeeFields(InputModel):
    display_name: Name
    role_id: UUID
    is_active: bool = True
    area_id: UUID | None = None
    all_houses: bool = False
    all_categories: bool = False
    house_ids: list[UUID] = Field(default_factory=list, max_length=500)
    category_ids: list[UUID] = Field(default_factory=list, max_length=500)

    @field_validator("house_ids", "category_ids")
    @classmethod
    def unique_ids(cls, value: list[UUID]) -> list[UUID]:
        return list(dict.fromkeys(value))


class EmployeeCreate(EmployeeFields):
    organization_id: UUID
    max_user_id: Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]


class EmployeeRead(ViewModel):
    id: UUID
    organization_id: UUID
    max_user_id: int
    display_name: str
    role_id: UUID
    is_active: bool
    area_id: UUID | None
    all_houses: bool
    all_categories: bool
    house_ids: list[UUID]
    category_ids: list[UUID]


class EmployeePatch(InputModel):
    display_name: Name | None = None
    role_id: UUID | None = None
    is_active: bool | None = None
    area_id: UUID | None = None
    all_houses: bool | None = None
    all_categories: bool | None = None
    house_ids: list[UUID] | None = Field(default=None, max_length=500)
    category_ids: list[UUID] | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def check_fields(self) -> "EmployeePatch":
        if not self.model_fields_set:
            raise ValueError("Provide at least one field")
        if any(getattr(self, key) is None for key in self.model_fields_set if key != "area_id"):
            raise ValueError("Only area_id can be null")
        return self


class StatusCreate(NamedCreate):
    code: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    is_initial: bool = False


class StatusRead(NamedRead):
    code: str
    is_initial: bool
