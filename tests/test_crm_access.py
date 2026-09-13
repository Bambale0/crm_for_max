"""Scope checks and tenant constraints against synthetic, isolated CRM data."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import AuthenticatedSession
from app.crm.access import SUPPORTED_PERMISSIONS, CRMAccess, load_access
from app.crm.errors import CRMNotFound, CRMPermissionDenied
from app.models.crm import (
    Area,
    Category,
    District,
    Employee,
    House,
    Organization,
    RequestStatus,
    Role,
)
from app.models.identity import AuthSession, User


def _access(organization_id: UUID) -> CRMAccess:
    return CRMAccess(
        organization_id=organization_id,
        is_owner=False,
        permissions=frozenset({"houses.view", "requests.view"}),
        area_id=None,
        all_houses=False,
        all_categories=False,
        house_ids=(),
        category_ids=(),
    )


def _authenticated(*, owner: bool = False, max_user_id: int = 202) -> AuthenticatedSession:
    user = User(id=uuid4(), max_user_id=max_user_id, display_name="Test employee", is_active=True)
    session = AuthSession(
        id=uuid4(),
        user_id=user.id,
        token_hash="a" * 64,
        init_data_hash="b" * 64,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    return AuthenticatedSession(user=user, session=session, is_owner=owner)


def test_empty_scopes_deny_even_with_permissions() -> None:
    access = _access(uuid4())
    house = House(id=uuid4(), organization_id=access.organization_id, area_id=uuid4(), address="A")
    assert not access.can_house(house)
    assert not access.can_category(uuid4())


def test_area_and_explicit_house_scopes_intersect() -> None:
    access = _access(uuid4())
    house = House(id=uuid4(), organization_id=access.organization_id, area_id=uuid4(), address="A")
    access = replace(access, area_id=house.area_id, house_ids=(house.id,))
    assert access.can_house(house)
    assert not replace(access, area_id=uuid4()).can_house(house)
    assert not replace(access, house_ids=(uuid4(),)).can_house(house)


def test_all_houses_keeps_area_and_organization_boundaries() -> None:
    access = replace(_access(uuid4()), area_id=uuid4(), all_houses=True)
    house = House(
        id=uuid4(), organization_id=access.organization_id, area_id=access.area_id, address="A"
    )
    assert access.can_house(house)
    house.area_id = uuid4()
    assert not access.can_house(house)
    house.area_id = access.area_id
    house.organization_id = uuid4()
    assert not access.can_house(house)


def test_category_scope_needs_an_explicit_broad_access_flag() -> None:
    category_id = uuid4()
    access = replace(_access(uuid4()), category_ids=(category_id,))
    assert access.can_category(category_id)
    assert not access.can_category(uuid4())
    assert replace(access, all_categories=True).can_category(uuid4())


def test_owner_bypasses_scopes_only_inside_selected_organization() -> None:
    access = replace(_access(uuid4()), is_owner=True, area_id=uuid4())
    house = House(id=uuid4(), organization_id=access.organization_id, area_id=uuid4(), address="A")
    assert access.can_house(house)
    assert access.can_category(uuid4())
    house.organization_id = uuid4()
    assert not access.can_house(house)


@dataclass
class Catalog:
    organization: Organization
    foreign_organization: Organization
    area: Area
    other_area: Area
    foreign_area: Area
    house: House
    other_house: House
    foreign_house: House
    category: Category
    role: Role
    employee: Employee


@pytest.fixture
async def catalog(db_session: AsyncSession) -> Catalog:
    organization = Organization(name="Synthetic management company")
    foreign_organization = Organization(name="Other synthetic management company")
    db_session.add_all([organization, foreign_organization])
    await db_session.flush()
    district = District(organization_id=organization.id, name="District")
    foreign_district = District(organization_id=foreign_organization.id, name="Foreign district")
    category = Category(organization_id=organization.id, name="Electricity")
    role = Role(
        organization_id=organization.id,
        name="Operator",
        permissions=["houses.view", "requests.view"],
    )
    db_session.add_all([district, foreign_district, category, role])
    await db_session.flush()
    area = Area(organization_id=organization.id, district_id=district.id, name="Area 1")
    other_area = Area(organization_id=organization.id, district_id=district.id, name="Area 2")
    foreign_area = Area(
        organization_id=foreign_organization.id, district_id=foreign_district.id, name="Other area"
    )
    db_session.add_all([area, other_area, foreign_area])
    await db_session.flush()
    house = House(organization_id=organization.id, area_id=area.id, address="Test 1")
    other_house = House(organization_id=organization.id, area_id=other_area.id, address="Test 2")
    foreign_house = House(
        organization_id=foreign_organization.id, area_id=foreign_area.id, address="Foreign test 1"
    )
    db_session.add_all([house, other_house, foreign_house])
    await db_session.flush()
    employee = Employee(
        organization_id=organization.id,
        max_user_id=202,
        display_name="Test operator",
        role_id=role.id,
        area_id=area.id,
        house_ids=[str(house.id)],
        category_ids=[str(category.id)],
    )
    db_session.add(employee)
    await db_session.flush()
    return Catalog(
        organization,
        foreign_organization,
        area,
        other_area,
        foreign_area,
        house,
        other_house,
        foreign_house,
        category,
        role,
        employee,
    )


@pytest.mark.integration
async def test_membership_loads_persisted_scope_and_sql_filters(
    db_session: AsyncSession, catalog: Catalog
) -> None:
    access = await load_access(db_session, _authenticated(), catalog.organization.id, "houses.view")
    houses = list(await db_session.scalars(select(House).where(access.house_predicate())))
    assert [house.id for house in houses] == [catalog.house.id]
    assert access.can_category(catalog.category.id)
    assert not access.can_house(catalog.other_house)
    assert not access.can_house(catalog.foreign_house)
    assert access.permissions == frozenset({"houses.view", "requests.view"})


@pytest.mark.integration
async def test_owner_still_needs_an_existing_organization(
    db_session: AsyncSession, catalog: Catalog
) -> None:
    authenticated = _authenticated(owner=True)
    access = await load_access(
        db_session, authenticated, catalog.organization.id, "requests.create"
    )
    house_ids = set(await db_session.scalars(select(House.id).where(access.house_predicate())))
    assert house_ids == {catalog.house.id, catalog.other_house.id}
    assert access.permissions == SUPPORTED_PERMISSIONS
    with pytest.raises(CRMNotFound):
        await load_access(db_session, authenticated, uuid4(), "requests.create")


@pytest.mark.integration
async def test_foreign_and_missing_organization_have_the_same_failure(
    db_session: AsyncSession, catalog: Catalog
) -> None:
    for organization_id in (catalog.foreign_organization.id, uuid4()):
        with pytest.raises(CRMNotFound):
            await load_access(db_session, _authenticated(), organization_id, "requests.view")


@pytest.mark.integration
async def test_role_permission_changes_apply_without_reauthenticating(
    db_session: AsyncSession, catalog: Catalog
) -> None:
    authenticated = _authenticated()
    await load_access(db_session, authenticated, catalog.organization.id, "requests.view")
    # Bypass identity-map synchronization to represent a change made by another request.
    await db_session.execute(
        update(Role)
        .where(Role.id == catalog.role.id)
        .values(permissions=["houses.view"])
        .execution_options(synchronize_session=False)
    )
    with pytest.raises(CRMPermissionDenied):
        await load_access(db_session, authenticated, catalog.organization.id, "requests.view")


@pytest.mark.integration
async def test_employee_deactivation_applies_to_existing_sessions(
    db_session: AsyncSession, catalog: Catalog
) -> None:
    authenticated = _authenticated()
    await load_access(db_session, authenticated, catalog.organization.id, "requests.view")
    await db_session.execute(
        update(Employee)
        .where(Employee.id == catalog.employee.id)
        .values(is_active=False)
        .execution_options(synchronize_session=False)
    )
    with pytest.raises(CRMNotFound):
        await load_access(db_session, authenticated, catalog.organization.id, "requests.view")


@pytest.mark.integration
async def test_empty_persisted_scope_does_not_become_all_houses(
    db_session: AsyncSession, catalog: Catalog
) -> None:
    catalog.employee.house_ids = []
    catalog.employee.category_ids = []
    await db_session.flush()
    access = await load_access(
        db_session, _authenticated(), catalog.organization.id, "requests.view"
    )
    assert list(await db_session.scalars(select(House.id).where(access.house_predicate()))) == []
    assert not access.can_category(catalog.category.id)
    catalog.employee.all_houses = True
    await db_session.flush()
    access = await load_access(
        db_session, _authenticated(), catalog.organization.id, "requests.view"
    )
    # Even all_houses does not override the area restriction.
    assert list(await db_session.scalars(select(House.id).where(access.house_predicate()))) == [
        catalog.house.id
    ]


@pytest.mark.integration
async def test_malformed_persisted_scope_fails_closed(
    db_session: AsyncSession, catalog: Catalog
) -> None:
    catalog.employee.house_ids = ["invalid-uuid"]
    await db_session.flush()
    with pytest.raises(CRMPermissionDenied):
        await load_access(db_session, _authenticated(), catalog.organization.id, "requests.view")


@pytest.mark.integration
async def test_database_rejects_house_linked_to_a_foreign_area(
    db_session: AsyncSession, catalog: Catalog
) -> None:
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                House(
                    organization_id=catalog.organization.id,
                    area_id=catalog.foreign_area.id,
                    address="Invalid tenant relation",
                )
            )
            await db_session.flush()


@pytest.mark.integration
async def test_database_rejects_employee_with_a_foreign_role(
    db_session: AsyncSession, catalog: Catalog
) -> None:
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                Employee(
                    organization_id=catalog.foreign_organization.id,
                    max_user_id=404,
                    display_name="Invalid tenant relation",
                    role_id=catalog.role.id,
                )
            )
            await db_session.flush()


@pytest.mark.integration
async def test_database_allows_only_one_initial_status_per_organization(
    db_session: AsyncSession, catalog: Catalog
) -> None:
    db_session.add(
        RequestStatus(
            organization_id=catalog.organization.id, code="new", name="Новая", is_initial=True
        )
    )
    await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                RequestStatus(
                    organization_id=catalog.organization.id,
                    code="duplicate",
                    name="Invalid initial status",
                    is_initial=True,
                )
            )
            await db_session.flush()
