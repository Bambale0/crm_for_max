"""Assignment and reports enforce current permissions and optimistic revisions."""

import asyncio
import os
from dataclasses import dataclass
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema

from app.auth.actor import Actor
from app.core.config import Settings
from app.crm.errors import CRMConflict, CRMInvalidReference, CRMNotFound, CRMPermissionDenied
from app.crm.task_workflow import assign_request, list_tasks, report_progress, visible_task
from app.models.base import Base
from app.models.crm import (
    Area,
    Category,
    District,
    Employee,
    House,
    Organization,
    RequestStatus,
    RequestStatusHistory,
    Role,
    ServiceRequest,
)
from app.models.identity import AuditLog, User
from app.models.task_progress import TaskProgress

pytestmark = pytest.mark.integration


@dataclass
class Work:
    owner: Actor
    worker: Actor
    employee: Employee
    role: Role
    task: ServiceRequest
    house: House


@pytest.fixture
async def work(db_session: AsyncSession) -> Work:
    return await _build_work(db_session)


async def _build_work(db_session: AsyncSession) -> Work:
    owner = User(max_user_id=101, display_name="Владелец")
    worker = User(max_user_id=202, display_name="Исполнитель")
    org = Organization(name="Тестовые задачи")
    db_session.add_all([owner, worker, org])
    await db_session.flush()
    district = District(organization_id=org.id, name="Район")
    db_session.add(district)
    await db_session.flush()
    area = Area(organization_id=org.id, district_id=district.id, name="Участок")
    category = Category(organization_id=org.id, name="Сантехника")
    role = Role(
        organization_id=org.id,
        name="Работник",
        permissions=["requests.view", "requests.update"],
    )
    initial = RequestStatus(organization_id=org.id, name="Новая", code="new", is_initial=True)
    db_session.add_all([area, category, role, initial])
    await db_session.flush()
    house = House(organization_id=org.id, area_id=area.id, address="Тестовая, 1")
    employee = Employee(
        organization_id=org.id,
        role_id=role.id,
        max_user_id=worker.max_user_id,
        display_name=worker.display_name,
        area_id=area.id,
        all_houses=True,
        category_ids=[str(category.id)],
    )
    db_session.add_all([house, employee])
    await db_session.flush()
    task = ServiceRequest(
        organization_id=org.id,
        house_id=house.id,
        category_id=category.id,
        status_id=initial.id,
        description="Заменить кран",
        priority="normal",
        created_by=owner.id,
        idempotency_key=uuid4(),
        payload_hash="0" * 64,
    )
    db_session.add(task)
    await db_session.flush()
    return Work(Actor(owner, True), Actor(worker, False), employee, role, task, house)


async def _assign(db: AsyncSession, work: Work) -> ServiceRequest:
    return await assign_request(
        db, work.owner, work.task.id, work.employee.id, work.task.revision, {101, 202}
    )


async def test_assignment_and_reports_preserve_history_and_trim_notes(
    db_session: AsyncSession, work: Work
) -> None:
    initial_id = work.task.status_id
    assigned = await _assign(db_session, work)
    assert assigned.assignee_id == work.employee.id
    assert assigned.revision == 1
    for revision, (state, note) in enumerate(
        [("in_progress", None), ("needs", "  Нужен новый кран  "), ("done", None)], start=1
    ):
        updated = await report_progress(
            db_session, work.worker, work.task.id, state, note, revision
        )
        status = await db_session.get(RequestStatus, updated.status_id)
        assert status is not None and status.code == state
        assert updated.revision == revision + 1
    history = list(
        (
            await db_session.scalars(
                select(RequestStatusHistory).where(RequestStatusHistory.request_id == work.task.id)
            )
        ).all()
    )
    assert len(history) == 3
    assert any(entry.from_status_id == initial_id for entry in history)
    assert all(entry.actor_id == work.worker.user.id for entry in history)
    progress = list((await db_session.scalars(select(TaskProgress))).all())
    assert len(progress) == 3
    assert next(entry.note for entry in progress if entry.state == "needs") == "Нужен новый кран"
    audits = list((await db_session.scalars(select(AuditLog))).all())
    assert [entry.action for entry in audits].count("request.assign") == 1
    assert [entry.action for entry in audits].count("request.progress") == 3
    assert all(entry.target_id == work.task.id for entry in audits)


@pytest.mark.parametrize("state", ["not_done", "needs"])
@pytest.mark.parametrize("note", [None, "", "   ", "x" * 2001])
async def test_failed_and_blocked_reports_require_bounded_explanation(
    db_session: AsyncSession, work: Work, state: str, note: str | None
) -> None:
    await _assign(db_session, work)
    with pytest.raises(CRMInvalidReference):
        await report_progress(db_session, work.worker, work.task.id, state, note, 1)
    assert work.task.revision == 1
    assert await db_session.scalar(select(func.count()).select_from(TaskProgress)) == 0


async def test_not_done_saves_reason_and_unknown_state_is_rejected(
    db_session: AsyncSession, work: Work
) -> None:
    await _assign(db_session, work)
    await report_progress(db_session, work.worker, work.task.id, "not_done", "Нет доступа", 1)
    progress = await db_session.scalar(select(TaskProgress))
    assert progress is not None and progress.note == "Нет доступа"
    with pytest.raises(CRMInvalidReference):
        await report_progress(db_session, work.worker, work.task.id, "invented", None, 2)


async def test_stale_buttons_cannot_overwrite_newer_assignment_or_report(
    db_session: AsyncSession, work: Work
) -> None:
    await _assign(db_session, work)
    with pytest.raises(CRMConflict):
        await assign_request(db_session, work.owner, work.task.id, work.employee.id, 0, {202})
    await report_progress(db_session, work.worker, work.task.id, "in_progress", None, 1)
    with pytest.raises(CRMConflict):
        await report_progress(db_session, work.worker, work.task.id, "done", None, 1)
    assert work.task.revision == 2
    assert await db_session.scalar(select(func.count()).select_from(TaskProgress)) == 1


async def test_only_assignee_or_owner_can_report_and_assign_requires_permission(
    db_session: AsyncSession, work: Work
) -> None:
    with pytest.raises(CRMPermissionDenied):
        await report_progress(db_session, work.worker, work.task.id, "done", None, 0)
    with pytest.raises(CRMPermissionDenied):
        await assign_request(db_session, work.worker, work.task.id, work.employee.id, 0, {202})
    await report_progress(db_session, work.owner, work.task.id, "done", None, 0)
    assert work.task.assignee_id is None
    assert work.task.revision == 1


@pytest.mark.parametrize("revocation", ["employee", "role", "house", "category", "area"])
async def test_reports_recheck_current_membership_and_scope(
    db_session: AsyncSession, work: Work, revocation: str
) -> None:
    await _assign(db_session, work)
    if revocation == "employee":
        work.employee.is_active = False
    elif revocation == "role":
        work.role.permissions = ["requests.view"]
    elif revocation == "house":
        work.employee.all_houses = False
    elif revocation == "category":
        work.employee.category_ids = []
    else:
        house_area = await db_session.get(Area, work.house.area_id)
        assert house_area is not None
        other_area = Area(
            organization_id=work.task.organization_id,
            district_id=house_area.district_id,
            name="Другой участок",
        )
        db_session.add(other_area)
        await db_session.flush()
        work.employee.area_id = other_area.id
    await db_session.flush()
    with pytest.raises((CRMNotFound, CRMPermissionDenied)):
        await report_progress(db_session, work.worker, work.task.id, "done", None, 1)
    assert await db_session.scalar(select(func.count()).select_from(TaskProgress)) == 0


@pytest.mark.parametrize(
    "invalid_target", ["env", "employee", "user", "role", "house", "category", "malformed"]
)
async def test_assignment_rejects_unusable_employee(
    db_session: AsyncSession, work: Work, invalid_target: str
) -> None:
    allowed = {202}
    if invalid_target == "env":
        allowed = set()
    elif invalid_target == "employee":
        work.employee.is_active = False
    elif invalid_target == "user":
        work.worker.user.is_active = False
    elif invalid_target == "role":
        work.role.permissions = ["requests.view"]
    elif invalid_target == "house":
        work.employee.all_houses = False
    elif invalid_target == "category":
        work.employee.category_ids = []
    else:
        work.employee.category_ids = ["invalid UUID"]
    await db_session.flush()
    with pytest.raises(CRMInvalidReference):
        await assign_request(db_session, work.owner, work.task.id, work.employee.id, 0, allowed)
    assert work.task.assignee_id is None
    assert work.task.revision == 0


async def test_assignment_and_visibility_reject_other_organization(
    db_session: AsyncSession, work: Work
) -> None:
    foreign_org = Organization(name="Другая УК")
    db_session.add(foreign_org)
    await db_session.flush()
    foreign_role = Role(
        organization_id=foreign_org.id,
        name="Работник",
        permissions=["requests.view", "requests.update"],
    )
    db_session.add(foreign_role)
    await db_session.flush()
    foreign_employee = Employee(
        organization_id=foreign_org.id,
        role_id=foreign_role.id,
        max_user_id=303,
        display_name="Другой работник",
        all_houses=True,
        all_categories=True,
    )
    foreign_user = User(max_user_id=303, display_name="Другой работник")
    db_session.add_all([foreign_employee, foreign_user])
    await db_session.flush()
    with pytest.raises(CRMInvalidReference):
        await assign_request(db_session, work.owner, work.task.id, foreign_employee.id, 0, {303})
    with pytest.raises(CRMNotFound):
        await visible_task(db_session, Actor(foreign_user, False), work.task.id)
    with pytest.raises(CRMNotFound):
        await visible_task(db_session, work.owner, uuid4())


async def test_my_tasks_filters_assignment_and_live_scope_before_pagination(
    db_session: AsyncSession, work: Work
) -> None:
    assert await list_tasks(db_session, work.worker, work.task.organization_id) == []
    assert await list_tasks(db_session, work.worker, work.task.organization_id, False) == [
        work.task
    ]
    await _assign(db_session, work)
    assert await list_tasks(db_session, work.worker, work.task.organization_id) == [work.task]
    assert await list_tasks(db_session, work.worker, work.task.organization_id, offset=1) == []
    work.employee.category_ids = []
    await db_session.flush()
    assert await list_tasks(db_session, work.worker, work.task.organization_id) == []
    with pytest.raises(CRMInvalidReference):
        await list_tasks(db_session, work.owner, work.task.organization_id, limit=101)


async def test_progress_is_rolled_back_with_callers_transaction(
    db_session: AsyncSession, work: Work
) -> None:
    request_id = work.task.id
    initial_id = work.task.status_id
    async with db_session.begin_nested() as savepoint:
        await _assign(db_session, work)
        await report_progress(db_session, work.worker, request_id, "done", None, 1)
        await savepoint.rollback()
    current = await db_session.get(ServiceRequest, request_id, populate_existing=True)
    assert current is not None
    assert current.assignee_id is None and current.revision == 0 and current.status_id == initial_id
    assert await db_session.scalar(select(func.count()).select_from(TaskProgress)) == 0
    assert await db_session.scalar(select(func.count()).select_from(RequestStatusHistory)) == 0
    assert await db_session.scalar(select(func.count()).select_from(AuditLog)) == 0
    assert await db_session.scalar(select(func.count()).select_from(RequestStatus)) == 1


async def test_concurrent_reports_accept_exactly_one_action_for_a_revision(
    test_settings: Settings,
) -> None:
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL must point to a disposable PostgreSQL database")
    # Isolate committed racing transactions without deleting rows or dropping schemas.
    schema_name = "task_concurrency_" + uuid4().hex
    engine = create_async_engine(
        test_settings.database_url.get_secret_value(),
        hide_parameters=True,
        execution_options={"schema_translate_map": {None: schema_name}},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    barrier = asyncio.Barrier(2)
    try:
        async with engine.begin() as connection:
            await connection.execute(CreateSchema(schema_name))
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as setup:
            work = await _build_work(setup)
            await _assign(setup, work)
            await setup.commit()

        async def attempt(state: str) -> int:
            async with factory() as db:
                await asyncio.wait_for(barrier.wait(), timeout=10)
                updated = await report_progress(db, work.worker, work.task.id, state, None, 1)
                await db.commit()
                return updated.revision

        results = await asyncio.wait_for(
            asyncio.gather(attempt("in_progress"), attempt("done"), return_exceptions=True),
            timeout=20,
        )
        assert results.count(2) == 1, results
        assert sum(isinstance(result, CRMConflict) for result in results) == 1, results
        async with factory() as verify:
            assert await verify.scalar(select(func.count()).select_from(TaskProgress)) == 1
            assert await verify.scalar(select(func.count()).select_from(RequestStatusHistory)) == 1
            assert await verify.scalar(select(func.count()).select_from(AuditLog)) == 2
            assert await verify.scalar(select(ServiceRequest.revision)) == 2
    finally:
        await engine.dispose()
