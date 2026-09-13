"""Public request contracts with bounded, normalized inputs."""

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

Priority = Literal["critical", "high", "normal", "low"]


class RequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    organization_id: UUID
    house_id: UUID
    entrance_id: UUID | None = None
    category_id: UUID
    applicant_name: Annotated[str | None, Field(max_length=200)] = None
    applicant_phone: Annotated[str | None, Field(max_length=50)] = None
    apartment: Annotated[str | None, Field(max_length=30)] = None
    description: Annotated[str, StringConstraints(min_length=1, max_length=10000)]
    priority: Priority = "normal"


class RequestFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    organization_id: UUID
    limit: Annotated[int, Field(ge=1, le=100)] = 50
    offset: Annotated[int, Field(ge=0, le=10000)] = 0
    house_id: UUID | None = None
    category_id: UUID | None = None
    status_id: UUID | None = None
    priority: Priority | None = None
    q: Annotated[str | None, Field(max_length=200)] = None
    created_from: AwareDatetime | None = None
    created_to: AwareDatetime | None = None

    @model_validator(mode="after")
    def ordered_dates(self) -> Self:
        if (
            self.created_from is not None
            and self.created_to is not None
            and self.created_from > self.created_to
        ):
            raise ValueError("created_from must not follow created_to")
        return self


class RequestRead(BaseModel):
    id: UUID
    number: int
    code: str
    organization_id: UUID
    house_id: UUID
    area_id: UUID
    entrance_id: UUID | None
    category_id: UUID
    status_id: UUID
    applicant_name: str | None
    applicant_phone: str | None
    apartment: str | None
    description: str
    priority: Priority
    source: Literal["manual", "staff_bot", "chat"]
    created_by: UUID
    created_at: datetime


class RequestPage(BaseModel):
    items: list[RequestRead]
    limit: int
    offset: int


class RequestHistoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    request_id: UUID
    from_status_id: UUID | None
    to_status_id: UUID
    actor_id: UUID
    created_at: datetime


class RequestHistoryPage(BaseModel):
    items: list[RequestHistoryRead]
