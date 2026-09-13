"""Assign requests and record employee reports in the caller's transaction."""

from collections.abc import Collection
from uuid import UUID, uuid4

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.auth.actor import ActorContext
from app.crm.access import access_from_membership, load_access
from app.crm.errors import CRMConflict, CRMInvalidReference, CRMNotFound, CRMPermissionDenied
from app.models.crm import (
    Employee,
    House,
    RequestStatus,
    RequestStatusHistory,
    Role,
    ServiceRequest,
)
from app.models.identity import AuditLog, User
from app.models.task_progress import TaskProgress

PROGRESS_STATES = {
    "in_progress": "В работе",
    "done": "Готово",
    "not_done": "Не выполнено",
    "needs": "Для выполнения нужно",
}

DISPATCH_QUEUES = {
    "new": "Новые",
    "unassigned": "Без исполнителя",
    "in_progress": "В работе",
    "attention": "Требуют внимания",
    "all": "Все заявки",
}


def _dispatch_condition(queue: str) -> ColumnElement[bool] | None:
    if queue == "new":
        return RequestStatus.code == "new"
    if queue == "unassigned":
        return and_(ServiceRequest.assignee_id.is_(None), RequestStatus.code != "done")
    if queue == "in_progress":
        return RequestStatus.code == "in_progress"
    if queue == "attention":
        return RequestStatus.code.in_(("needs", "not_done"))
    if queue == "all":
        return None
    raise CRMInvalidReference


async def dispatcher_counts(
    db: AsyncSession,
    actor: ActorContext,
    org_id: UUID,
) -> dict[str, int]:
    access = await load_access(db, actor, org_id, "requests.assign")
    row = (
        await db.execute(
            select(
                func.count().filter(RequestStatus.code == "new"),
                func.count().filter(
                    and_(ServiceRequest.assignee_id.is_(None), RequestStatus.code != "done")
                ),
                func.count().filter(RequestStatus.code == "in_progress"),
                func.count().filter(RequestStatus.code.in_(("needs", "not_done"))),
                func.count(),
            )
            .select_from(ServiceRequest)
            .join(House, House.id == ServiceRequest.house_id)
            .join(RequestStatus, RequestStatus.id == ServiceRequest.status_id)
            .where(access.house_predicate(), access.category_predicate())
        )
    ).one()
    return {
        "new": int(row[0] or 0),
        "unassigned": int(row[1] or 0),
        "in_progress": int(row[2] or 0),
        "attention": int(row[3] or 0),
        "all": int(row[4] or 0),
    }


async def list_dispatch_tasks(
    db: AsyncSession,
    actor: ActorContext,
    org_id: UUID,
    queue: str,
    *,
    limit: int = 10,
    offset: int = 0,
) -> list[ServiceRequest]:
    if not 1 <= limit <= 100 or not 0 <= offset <= 10_000:
        raise CRMInvalidReference
    condition = _dispatch_condition(queue)
    access = await load_access(db, actor, org_id, "requests.assign")
    statement = (
        select(ServiceRequest)
        .join(House, House.id == ServiceRequest.house_id)
        .join(RequestStatus, RequestStatus.id == ServiceRequest.status_id)
        .where(access.house_predicate(), access.category_predicate())
        .order_by(ServiceRequest.created_at.desc(), ServiceRequest.number.desc())
        .limit(limit)
        .offset(offset)
        .execution_options(populate_existing=True)
    )
    if condition is not None:
        statement = statement.where(condition)
    return list((await db.scalars(statement)).all())


async def _visible_task(
    db: AsyncSession,
    actor: ActorContext,
    request_id: UUID,
    *,
    permission: str = "requests.view",
    lock: bool = False,
) -> ServiceRequest:
    statement = (
        select(ServiceRequest, House)
        .join(
            House,
            and_(
                House.id == ServiceRequest.house_id,
                House.organization_id == ServiceRequest.organization_id,
            ),
        )
        .where(ServiceRequest.id == request_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update(of=ServiceRequest)
    row = (await db.execute(statement)).one_or_none()
    if row is None:
        raise CRMNotFound
    task: ServiceRequest = row[0]
    house: House = row[1]
    access = await load_access(db, actor, task.organization_id, permission)
    if "requests.view" not in access.permissions:
        raise CRMPermissionDenied
    if not access.can_house(house) or not access.can_category(task.category_id):
        raise CRMNotFound
    return task


async def visible_task(db: AsyncSession, actor: ActorContext, request_id: UUID) -> ServiceRequest:
    return await _visible_task(db, actor, request_id)


async def list_tasks(
    db: AsyncSession,
    actor: ActorContext,
    org_id: UUID,
    only_mine: bool = True,
    limit: int = 10,
    offset: int = 0,
) -> list[ServiceRequest]:
    if not 1 <= limit <= 100 or not 0 <= offset <= 10_000:
        raise CRMInvalidReference
    access = await load_access(db, actor, org_id, "requests.view")
    statement = (
        select(ServiceRequest)
        .join(House, House.id == ServiceRequest.house_id)
        .where(access.house_predicate(), access.category_predicate())
        .order_by(ServiceRequest.created_at.desc(), ServiceRequest.id.desc())
        .limit(limit)
        .offset(offset)
        .execution_options(populate_existing=True)
    )
    if only_mine:
        statement = statement.join(Employee, Employee.id == ServiceRequest.assignee_id).where(
            Employee.organization_id == org_id,
            Employee.max_user_id == actor.user.max_user_id,
            Employee.is_active.is_(True),
        )
    return list((await db.scalars(statement)).all())


async def assign_request(
    db: AsyncSession,
    actor: ActorContext,
    request_id: UUID,
    employee_id: UUID,
    expected_revision: int,
    allowed_max_ids: Collection[int],
) -> ServiceRequest:
    task = await _visible_task(db, actor, request_id, permission="requests.assign", lock=True)
    if task.revision != expected_revision:
        raise CRMConflict
    row = (
        await db.execute(
            select(Employee, Role)
            .join(
                Role,
                and_(Role.id == Employee.role_id, Role.organization_id == Employee.organization_id),
            )
            .where(Employee.id == employee_id, Employee.organization_id == task.organization_id)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        raise CRMInvalidReference
    employee, role = row
    if employee.max_user_id not in allowed_max_ids or not employee.is_active:
        raise CRMInvalidReference
    disabled_user = await db.scalar(
        select(User.id).where(User.max_user_id == employee.max_user_id, User.is_active.is_(False))
    )
    if disabled_user is not None:
        raise CRMInvalidReference
    try:
        access = access_from_membership(employee, role)
    except CRMPermissionDenied:
        raise CRMInvalidReference from None
    house = await db.get(House, task.house_id, populate_existing=True)
    if (
        house is None
        or not {"requests.view", "requests.update"} <= access.permissions
        or not access.can_house(house)
        or not access.can_category(task.category_id)
    ):
        raise CRMInvalidReference
    task.assignee_id = employee.id
    task.revision += 1
    db.add(
        AuditLog(
            actor_id=actor.user.id,
            action="request.assign",
            target_type="requests",
            target_id=task.id,
        )
    )
    await db.flush()
    return task


async def report_progress(
    db: AsyncSession,
    actor: ActorContext,
    request_id: UUID,
    state: str,
    note: str | None,
    expected_revision: int,
) -> ServiceRequest:
    normalized_note = note.strip() if note is not None else None
    if (
        state not in PROGRESS_STATES
        or (normalized_note is not None and len(normalized_note) > 2000)
        or (state in {"not_done", "needs"} and not normalized_note)
    ):
        raise CRMInvalidReference
    normalized_note = normalized_note or None
    task = await _visible_task(db, actor, request_id, permission="requests.update", lock=True)
    if not actor.is_owner:
        assignee_id = await db.scalar(
            select(Employee.id).where(
                Employee.id == task.assignee_id,
                Employee.organization_id == task.organization_id,
                Employee.max_user_id == actor.user.max_user_id,
                Employee.is_active.is_(True),
            )
        )
        if assignee_id is None:
            raise CRMPermissionDenied
    if task.revision != expected_revision:
        raise CRMConflict
    status_id = await db.scalar(
        insert(RequestStatus)
        .values(
            id=uuid4(),
            organization_id=task.organization_id,
            code=state,
            name=PROGRESS_STATES[state],
            is_initial=False,
        )
        .on_conflict_do_nothing(constraint="uq_request_statuses_code")
        .returning(RequestStatus.id)
    )
    if status_id is None:
        status_id = await db.scalar(
            select(RequestStatus.id).where(
                RequestStatus.organization_id == task.organization_id, RequestStatus.code == state
            )
        )
    if status_id is None:
        raise CRMConflict
    if task.status_id != status_id:
        db.add(
            RequestStatusHistory(
                request_id=task.id,
                from_status_id=task.status_id,
                to_status_id=status_id,
                actor_id=actor.user.id,
            )
        )
    task.status_id = status_id
    task.revision += 1
    db.add_all(
        [
            TaskProgress(
                request_id=task.id, actor_id=actor.user.id, state=state, note=normalized_note
            ),
            AuditLog(
                actor_id=actor.user.id,
                action="request.progress",
                target_type="requests",
                target_id=task.id,
            ),
        ]
    )
    await db.flush()
    return task
