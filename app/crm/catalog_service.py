"""Catalog persistence and validation, shared by the HTTP adapters."""

from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.crm.catalog_schemas import EmployeeFields
from app.crm.errors import CRMConflict, CRMInvalidReference
from app.models.crm import (
    Area,
    Category,
    District,
    Employee,
    Entrance,
    House,
    Organization,
    RequestStatus,
    Role,
)
from app.models.identity import AuditLog

type CatalogEntity = (
    Organization | District | Area | House | Entrance | Category | Role | Employee | RequestStatus
)


async def save[M: CatalogEntity](
    db: AsyncSession,
    entity: M,
    *,
    actor_id: UUID,
    operation: Literal["create", "update"] = "create",
) -> M:
    db.add(entity)
    try:
        await db.flush()
        db.add(
            AuditLog(
                actor_id=actor_id,
                action=f"catalog.{operation}",
                target_type=entity.__tablename__,
                target_id=entity.id,
            )
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise CRMConflict("Catalog entry conflicts with existing data") from None
    await db.refresh(entity)
    return entity


async def organization_exists(db: AsyncSession, organization_id: UUID) -> None:
    if await db.get(Organization, organization_id) is None:
        raise CRMInvalidReference("Invalid organization reference")


async def same_organization[M: CatalogEntity](
    db: AsyncSession, model: type[M], entity_id: UUID, organization_id: UUID
) -> M:
    entity = await db.get(model, entity_id)
    if entity is None or getattr(entity, "organization_id", None) != organization_id:
        raise CRMInvalidReference("Invalid catalog reference")
    return entity


async def validate_employee_scope(
    db: AsyncSession, organization_id: UUID, fields: EmployeeFields
) -> None:
    await same_organization(db, Role, fields.role_id, organization_id)
    if fields.area_id is not None:
        await same_organization(db, Area, fields.area_id, organization_id)
    if fields.house_ids:
        houses = list(
            await db.scalars(
                select(House).where(
                    House.id.in_(fields.house_ids), House.organization_id == organization_id
                )
            )
        )
        if len(houses) != len(set(fields.house_ids)) or any(
            fields.area_id is not None and house.area_id != fields.area_id for house in houses
        ):
            raise CRMInvalidReference("House scope does not match the organization and area")
    if fields.category_ids:
        categories = list(
            await db.scalars(
                select(Category.id).where(
                    Category.id.in_(fields.category_ids),
                    Category.organization_id == organization_id,
                )
            )
        )
        if len(categories) != len(set(fields.category_ids)):
            raise CRMInvalidReference("Category scope does not match the organization")


def employee_values(fields: EmployeeFields) -> dict[str, Any]:
    values = fields.model_dump()
    values["house_ids"] = [str(item) for item in fields.house_ids]
    values["category_ids"] = [str(item) for item in fields.category_ids]
    return values
