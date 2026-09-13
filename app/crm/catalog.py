"""Owner-managed catalogs and scope-filtered employee reads."""

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.auth.routes import DatabaseDependency, SessionDependency, require_owner
from app.auth.service import AuthenticatedSession
from app.crm.access import SUPPORTED_PERMISSIONS, load_access
from app.crm.catalog_schemas import (
    AreaCreate,
    AreaRead,
    EmployeeCreate,
    EmployeeFields,
    EmployeePatch,
    EmployeeRead,
    EntranceCreate,
    EntranceRead,
    HouseCreate,
    HouseRead,
    NamedCreate,
    NamedRead,
    OrganizationCreate,
    OrganizationRead,
    Page,
    RoleCreate,
    RolePatch,
    RoleRead,
    StatusCreate,
    StatusRead,
)
from app.crm.catalog_service import (
    employee_values,
    organization_exists,
    same_organization,
    save,
    validate_employee_scope,
)
from app.crm.errors import CRMNotFound
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

router = APIRouter(prefix="/api", tags=["catalog"])
Owner = Annotated[AuthenticatedSession, Depends(require_owner)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=10000)]


@router.get("/permissions")
async def permissions(owner: Owner) -> dict[str, list[str]]:
    return {"items": sorted(SUPPORTED_PERMISSIONS)}


@router.post("/organizations", response_model=OrganizationRead, status_code=201)
async def create_organization(
    payload: OrganizationCreate, db: DatabaseDependency, owner: Owner
) -> Organization:
    entity = Organization(id=uuid4(), name=payload.name)
    db.add(entity)
    await db.flush()
    db.add(RequestStatus(organization_id=entity.id, code="new", name="Новая", is_initial=True))
    return await save(db, entity, actor_id=owner.user.id)


@router.get("/organizations", response_model=Page[OrganizationRead])
async def organizations(
    db: DatabaseDependency, auth: SessionDependency, limit: Limit = 50, offset: Offset = 0
) -> Page[OrganizationRead]:
    query = select(Organization)
    if not auth.is_owner:
        query = query.where(
            Organization.id.in_(
                select(Employee.organization_id).where(
                    Employee.max_user_id == auth.user.max_user_id, Employee.is_active.is_(True)
                )
            )
        )
    rows = await db.scalars(
        query.order_by(Organization.name, Organization.id).limit(limit).offset(offset)
    )
    return Page(
        items=[OrganizationRead.model_validate(row) for row in rows], limit=limit, offset=offset
    )


@router.post("/districts", response_model=NamedRead, status_code=201)
async def create_district(payload: NamedCreate, db: DatabaseDependency, owner: Owner) -> District:
    await organization_exists(db, payload.organization_id)
    return await save(db, District(**payload.model_dump()), actor_id=owner.user.id)


@router.get("/districts", response_model=Page[NamedRead])
async def districts(
    organization_id: UUID,
    db: DatabaseDependency,
    auth: SessionDependency,
    limit: Limit = 50,
    offset: Offset = 0,
) -> Page[NamedRead]:
    access = await load_access(db, auth, organization_id, "houses.view")
    query = select(District).where(District.organization_id == organization_id)
    if not access.is_owner:
        query = query.where(
            District.id.in_(
                select(Area.district_id)
                .join(House, House.area_id == Area.id)
                .where(access.house_predicate())
            )
        )
    rows = await db.scalars(query.order_by(District.name, District.id).limit(limit).offset(offset))
    return Page(items=[NamedRead.model_validate(row) for row in rows], limit=limit, offset=offset)


@router.post("/areas", response_model=AreaRead, status_code=201)
async def create_area(payload: AreaCreate, db: DatabaseDependency, owner: Owner) -> Area:
    await same_organization(db, District, payload.district_id, payload.organization_id)
    return await save(db, Area(**payload.model_dump()), actor_id=owner.user.id)


@router.get("/areas", response_model=Page[AreaRead])
async def areas(
    organization_id: UUID,
    db: DatabaseDependency,
    auth: SessionDependency,
    limit: Limit = 50,
    offset: Offset = 0,
) -> Page[AreaRead]:
    access = await load_access(db, auth, organization_id, "houses.view")
    query = select(Area).where(Area.organization_id == organization_id)
    if not access.is_owner:
        query = query.where(Area.id.in_(select(House.area_id).where(access.house_predicate())))
    rows = await db.scalars(query.order_by(Area.name, Area.id).limit(limit).offset(offset))
    return Page(items=[AreaRead.model_validate(row) for row in rows], limit=limit, offset=offset)


@router.post("/houses", response_model=HouseRead, status_code=201)
async def create_house(payload: HouseCreate, db: DatabaseDependency, owner: Owner) -> House:
    await same_organization(db, Area, payload.area_id, payload.organization_id)
    return await save(db, House(**payload.model_dump()), actor_id=owner.user.id)


@router.get("/houses", response_model=Page[HouseRead])
async def houses(
    organization_id: UUID,
    db: DatabaseDependency,
    auth: SessionDependency,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> Page[HouseRead]:
    access = await load_access(db, auth, organization_id, "houses.view")
    query = select(House).where(access.house_predicate())
    if q:
        query = query.where(House.address.icontains(q, autoescape=True))
    rows = await db.scalars(query.order_by(House.address, House.id).limit(limit).offset(offset))
    return Page(items=[HouseRead.model_validate(row) for row in rows], limit=limit, offset=offset)


@router.post("/entrances", response_model=EntranceRead, status_code=201)
async def create_entrance(
    payload: EntranceCreate, db: DatabaseDependency, owner: Owner
) -> Entrance:
    await same_organization(db, House, payload.house_id, payload.organization_id)
    return await save(db, Entrance(**payload.model_dump()), actor_id=owner.user.id)


@router.get("/entrances", response_model=Page[EntranceRead])
async def entrances(
    organization_id: UUID,
    db: DatabaseDependency,
    auth: SessionDependency,
    house_id: UUID | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> Page[EntranceRead]:
    access = await load_access(db, auth, organization_id, "houses.view")
    query = (
        select(Entrance).join(House, Entrance.house_id == House.id).where(access.house_predicate())
    )
    if house_id is not None:
        query = query.where(Entrance.house_id == house_id)
    rows = await db.scalars(
        query.order_by(Entrance.number, Entrance.id).limit(limit).offset(offset)
    )
    return Page(
        items=[EntranceRead.model_validate(row) for row in rows], limit=limit, offset=offset
    )


@router.post("/categories", response_model=NamedRead, status_code=201)
async def create_category(payload: NamedCreate, db: DatabaseDependency, owner: Owner) -> Category:
    await organization_exists(db, payload.organization_id)
    return await save(db, Category(**payload.model_dump()), actor_id=owner.user.id)


@router.get("/categories", response_model=Page[NamedRead])
async def categories(
    organization_id: UUID,
    db: DatabaseDependency,
    auth: SessionDependency,
    limit: Limit = 50,
    offset: Offset = 0,
) -> Page[NamedRead]:
    access = await load_access(db, auth, organization_id, "requests.view")
    query = select(Category).where(Category.organization_id == organization_id)
    if not (access.is_owner or access.all_categories):
        query = query.where(Category.id.in_(access.category_ids))
    rows = await db.scalars(query.order_by(Category.name, Category.id).limit(limit).offset(offset))
    return Page(items=[NamedRead.model_validate(row) for row in rows], limit=limit, offset=offset)


@router.post("/roles", response_model=RoleRead, status_code=201)
async def create_role(payload: RoleCreate, db: DatabaseDependency, owner: Owner) -> Role:
    await organization_exists(db, payload.organization_id)
    return await save(db, Role(**payload.model_dump()), actor_id=owner.user.id)


@router.get("/roles", response_model=Page[RoleRead])
async def roles(
    organization_id: UUID,
    db: DatabaseDependency,
    owner: Owner,
    limit: Limit = 50,
    offset: Offset = 0,
) -> Page[RoleRead]:
    await load_access(db, owner, organization_id, "houses.view")
    rows = await db.scalars(
        select(Role)
        .where(Role.organization_id == organization_id)
        .order_by(Role.name, Role.id)
        .limit(limit)
        .offset(offset)
    )
    return Page(items=[RoleRead.model_validate(row) for row in rows], limit=limit, offset=offset)


@router.patch("/roles/{role_id}", response_model=RoleRead)
async def update_role(
    role_id: UUID,
    organization_id: UUID,
    payload: RolePatch,
    db: DatabaseDependency,
    owner: Owner,
) -> Role:
    entity = await db.scalar(
        select(Role)
        .where(Role.id == role_id, Role.organization_id == organization_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if entity is None:
        raise CRMNotFound
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(entity, key, value)
    return await save(db, entity, actor_id=owner.user.id, operation="update")


@router.post("/employees", response_model=EmployeeRead, status_code=201)
async def create_employee(
    payload: EmployeeCreate, db: DatabaseDependency, owner: Owner
) -> Employee:
    fields = EmployeeFields.model_validate(
        payload.model_dump(exclude={"organization_id", "max_user_id"})
    )
    await validate_employee_scope(db, payload.organization_id, fields)
    entity = Employee(
        organization_id=payload.organization_id,
        max_user_id=payload.max_user_id,
        **employee_values(fields),
    )
    return await save(db, entity, actor_id=owner.user.id)


@router.get("/employees", response_model=Page[EmployeeRead])
async def employees(
    organization_id: UUID,
    db: DatabaseDependency,
    auth: SessionDependency,
    limit: Limit = 50,
    offset: Offset = 0,
) -> Page[EmployeeRead]:
    access = await load_access(db, auth, organization_id, "employees.view")
    query = select(Employee).where(Employee.organization_id == organization_id)
    if not access.is_owner:
        query = query.where(Employee.max_user_id == auth.user.max_user_id)
    rows = await db.scalars(
        query.order_by(Employee.display_name, Employee.id).limit(limit).offset(offset)
    )
    return Page(
        items=[EmployeeRead.model_validate(row) for row in rows], limit=limit, offset=offset
    )


@router.patch("/employees/{employee_id}", response_model=EmployeeRead)
async def update_employee(
    employee_id: UUID,
    organization_id: UUID,
    payload: EmployeePatch,
    db: DatabaseDependency,
    owner: Owner,
) -> Employee:
    entity = await db.scalar(
        select(Employee)
        .where(Employee.id == employee_id, Employee.organization_id == organization_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if entity is None:
        raise CRMNotFound
    values = EmployeeRead.model_validate(entity).model_dump(
        exclude={"id", "organization_id", "max_user_id"}
    )
    values.update(payload.model_dump(exclude_unset=True))
    fields = EmployeeFields.model_validate(values)
    await validate_employee_scope(db, organization_id, fields)
    for key, value in employee_values(fields).items():
        setattr(entity, key, value)
    return await save(db, entity, actor_id=owner.user.id, operation="update")


@router.post("/statuses", response_model=StatusRead, status_code=201)
async def create_status(
    payload: StatusCreate, db: DatabaseDependency, owner: Owner
) -> RequestStatus:
    await organization_exists(db, payload.organization_id)
    return await save(db, RequestStatus(**payload.model_dump()), actor_id=owner.user.id)


@router.get("/statuses", response_model=Page[StatusRead])
async def statuses(
    organization_id: UUID,
    db: DatabaseDependency,
    auth: SessionDependency,
    limit: Limit = 50,
    offset: Offset = 0,
) -> Page[StatusRead]:
    await load_access(db, auth, organization_id, "requests.view")
    rows = await db.scalars(
        select(RequestStatus)
        .where(RequestStatus.organization_id == organization_id)
        .order_by(RequestStatus.name, RequestStatus.id)
        .limit(limit)
        .offset(offset)
    )
    return Page(items=[StatusRead.model_validate(row) for row in rows], limit=limit, offset=offset)
