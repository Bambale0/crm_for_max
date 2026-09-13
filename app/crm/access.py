"""Load current membership and combine permission, house and category boundaries."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.auth.actor import ActorContext
from app.crm.errors import CRMNotFound, CRMPermissionDenied
from app.models.crm import Employee, House, Organization, Role, ServiceRequest

SUPPORTED_PERMISSIONS = frozenset(
    {
        "houses.view",
        "employees.view",
        "requests.view",
        "requests.create",
        "requests.assign",
        "requests.update",
    }
)


@dataclass(frozen=True)
class CRMAccess:
    organization_id: UUID
    is_owner: bool
    permissions: frozenset[str]
    area_id: UUID | None
    all_houses: bool
    all_categories: bool
    house_ids: tuple[UUID, ...]
    category_ids: tuple[UUID, ...]

    def can_house(self, house: House) -> bool:
        if house.organization_id != self.organization_id:
            return False
        if self.is_owner:
            return True
        return (self.area_id is None or house.area_id == self.area_id) and (
            self.all_houses or house.id in self.house_ids
        )

    def can_category(self, category_id: UUID) -> bool:
        # The caller validates the category's organization before checking its scope.
        return self.is_owner or self.all_categories or category_id in self.category_ids

    def house_predicate(self) -> ColumnElement[bool]:
        conditions: list[ColumnElement[bool]] = [House.organization_id == self.organization_id]
        if not self.is_owner:
            if self.area_id is not None:
                conditions.append(House.area_id == self.area_id)
            if not self.all_houses:
                conditions.append(House.id.in_(self.house_ids))
        return and_(*conditions)

    def category_predicate(self) -> ColumnElement[bool]:
        organization = ServiceRequest.organization_id == self.organization_id
        if self.is_owner or self.all_categories:
            return organization
        return and_(organization, ServiceRequest.category_id.in_(self.category_ids))


async def load_access(
    db: AsyncSession,
    authenticated: ActorContext,
    organization_id: UUID,
    permission: str,
) -> CRMAccess:
    if permission not in SUPPORTED_PERMISSIONS:
        raise CRMPermissionDenied
    if authenticated.is_owner:
        if (
            await db.scalar(select(Organization.id).where(Organization.id == organization_id))
            is None
        ):
            raise CRMNotFound
        return CRMAccess(
            organization_id=organization_id,
            is_owner=True,
            permissions=SUPPORTED_PERMISSIONS,
            area_id=None,
            all_houses=True,
            all_categories=True,
            house_ids=(),
            category_ids=(),
        )
    row = (
        await db.execute(
            select(Employee, Role)
            .join(
                Role,
                and_(Role.id == Employee.role_id, Role.organization_id == Employee.organization_id),
            )
            .where(
                Employee.organization_id == organization_id,
                Employee.max_user_id == authenticated.user.max_user_id,
                Employee.is_active.is_(True),
            )
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        # An existing foreign organization and a missing organization look identical.
        raise CRMNotFound
    employee, role = row
    access = access_from_membership(employee, role)
    if permission not in access.permissions:
        raise CRMPermissionDenied
    return access


def access_from_membership(employee: Employee, role: Role) -> CRMAccess:
    """Interpret an existing membership using the same fail-closed scope rules."""
    if (
        not employee.is_active
        or employee.organization_id != role.organization_id
        or employee.role_id != role.id
    ):
        raise CRMPermissionDenied
    try:
        permissions = frozenset(role.permissions) & SUPPORTED_PERMISSIONS
        house_ids = tuple(UUID(value) for value in employee.house_ids)
        category_ids = tuple(UUID(value) for value in employee.category_ids)
    except (TypeError, ValueError, AttributeError):
        # Unexpected persisted scope data must fail closed, including manual DB edits.
        raise CRMPermissionDenied from None
    return CRMAccess(
        organization_id=employee.organization_id,
        is_owner=False,
        permissions=permissions,
        area_id=employee.area_id,
        all_houses=employee.all_houses,
        all_categories=employee.all_categories,
        house_ids=house_ids,
        category_ids=category_ids,
    )
