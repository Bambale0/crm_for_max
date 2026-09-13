"""CRM persistence with organization boundaries enforced by composite foreign keys."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class District(Base):
    __tablename__ = "districts"
    __table_args__ = (UniqueConstraint("organization_id", "id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(200))


class Area(Base):
    __tablename__ = "areas"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "district_id"],
            ["districts.organization_id", "districts.id"],
            name="fk_areas_organization_district",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    district_id: Mapped[UUID] = mapped_column()
    name: Mapped[str] = mapped_column(String(200))


class House(Base):
    __tablename__ = "houses"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "area_id"],
            ["areas.organization_id", "areas.id"],
            name="fk_houses_organization_area",
        ),
        Index("ix_houses_organization_area", "organization_id", "area_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    area_id: Mapped[UUID] = mapped_column()
    address: Mapped[str] = mapped_column(String(500))


class Entrance(Base):
    __tablename__ = "entrances"
    __table_args__ = (
        UniqueConstraint("organization_id", "house_id", "id"),
        UniqueConstraint("organization_id", "house_id", "number", name="uq_entrances_number"),
        ForeignKeyConstraint(
            ["organization_id", "house_id"],
            ["houses.organization_id", "houses.id"],
            name="fk_entrances_organization_house",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    house_id: Mapped[UUID] = mapped_column()
    number: Mapped[str] = mapped_column(String(20))


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("organization_id", "id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(200))


class Role(Base):
    __tablename__ = "roles"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        CheckConstraint("jsonb_typeof(permissions) = 'array'", name="permissions_array"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(200))
    permissions: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )


class Employee(Base):
    __tablename__ = "employees"
    __table_args__ = (
        UniqueConstraint("organization_id", "max_user_id"),
        ForeignKeyConstraint(
            ["organization_id", "role_id"],
            ["roles.organization_id", "roles.id"],
            name="fk_employees_organization_role",
        ),
        ForeignKeyConstraint(
            ["organization_id", "area_id"],
            ["areas.organization_id", "areas.id"],
            name="fk_employees_organization_area",
        ),
        CheckConstraint("max_user_id > 0", name="positive_max_user_id"),
        CheckConstraint("jsonb_typeof(house_ids) = 'array'", name="house_ids_array"),
        CheckConstraint("jsonb_typeof(category_ids) = 'array'", name="category_ids_array"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    max_user_id: Mapped[int] = mapped_column(BigInteger)
    display_name: Mapped[str] = mapped_column(String(200))
    role_id: Mapped[UUID] = mapped_column()
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    area_id: Mapped[UUID | None] = mapped_column()
    all_houses: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    all_categories: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    house_ids: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    category_ids: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )


class RequestStatus(Base):
    __tablename__ = "request_statuses"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "code", name="uq_request_statuses_code"),
        Index(
            "uq_request_statuses_initial",
            "organization_id",
            unique=True,
            postgresql_where=text("is_initial"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(200))
    code: Mapped[str] = mapped_column(String(64))
    is_initial: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class ServiceRequest(Base):
    __tablename__ = "requests"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "created_by", "idempotency_key", name="uq_requests_idempotency"
        ),
        ForeignKeyConstraint(
            ["organization_id", "house_id"],
            ["houses.organization_id", "houses.id"],
            name="fk_requests_organization_house",
        ),
        ForeignKeyConstraint(
            ["organization_id", "house_id", "entrance_id"],
            ["entrances.organization_id", "entrances.house_id", "entrances.id"],
            name="fk_requests_organization_house_entrance",
        ),
        ForeignKeyConstraint(
            ["organization_id", "category_id"],
            ["categories.organization_id", "categories.id"],
            name="fk_requests_organization_category",
        ),
        ForeignKeyConstraint(
            ["organization_id", "status_id"],
            ["request_statuses.organization_id", "request_statuses.id"],
            name="fk_requests_organization_status",
        ),
        Index("ix_requests_organization_created", "organization_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    number: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    house_id: Mapped[UUID] = mapped_column()
    entrance_id: Mapped[UUID | None] = mapped_column()
    category_id: Mapped[UUID] = mapped_column()
    status_id: Mapped[UUID] = mapped_column()
    applicant_name: Mapped[str | None] = mapped_column(String(200))
    applicant_phone: Mapped[str | None] = mapped_column(String(50))
    apartment: Mapped[str | None] = mapped_column(String(30))
    description: Mapped[str] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(16), default="manual", server_default="manual")
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    idempotency_key: Mapped[UUID] = mapped_column()
    payload_hash: Mapped[str] = mapped_column(String(64))


class RequestStatusHistory(Base):
    __tablename__ = "request_status_history"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(ForeignKey("requests.id"), index=True)
    from_status_id: Mapped[UUID | None] = mapped_column(ForeignKey("request_statuses.id"))
    to_status_id: Mapped[UUID] = mapped_column(ForeignKey("request_statuses.id"))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
