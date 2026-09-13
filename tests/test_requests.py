"""Manual intake, idempotency and scoped reads against real PostgreSQL."""

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import FlushError
from sqlalchemy.schema import CreateSchema
from sqlalchemy.sql.dml import Insert

from app.auth.service import AuthenticatedSession
from app.core.config import Settings
from app.crm.errors import CRMConflict
from app.crm.request_schemas import RequestCreate
from app.crm.requests import create_manual_request
from app.models.base import Base
from app.models.crm import (
    Area,
    Category,
    District,
    Employee,
    Entrance,
    House,
    Organization,
    RequestStatus,
    RequestStatusHistory,
    Role,
    ServiceRequest,
)
from app.models.identity import AuditLog, AuthSession, User

pytestmark = pytest.mark.integration


@dataclass
class Catalog:
    organization: Organization
    area: Area
    other_area: Area
    house: House
    other_house: House
    entrance: Entrance
    other_entrance: Entrance
    category: Category
    other_category: Category
    status: RequestStatus
    role: Role
    employee: Employee


@pytest.fixture
async def catalog(db_session: AsyncSession) -> Catalog:
    return await _build_catalog(db_session)


async def _build_catalog(db_session: AsyncSession) -> Catalog:
    organization = Organization(name="Synthetic request test organization")
    db_session.add(organization)
    await db_session.flush()
    district = District(organization_id=organization.id, name="Северный район")
    db_session.add(district)
    await db_session.flush()
    area = Area(organization_id=organization.id, district_id=district.id, name="Участок 1")
    other_area = Area(organization_id=organization.id, district_id=district.id, name="Участок 2")
    category = Category(organization_id=organization.id, name="Сантехника")
    other_category = Category(organization_id=organization.id, name="Электрика")
    initial = RequestStatus(
        organization_id=organization.id, name="Новая", code="new", is_initial=True
    )
    role = Role(
        organization_id=organization.id,
        name="Оператор",
        permissions=["requests.create", "requests.view", "houses.view"],
    )
    db_session.add_all([area, other_area, category, other_category, initial, role])
    await db_session.flush()
    house = House(organization_id=organization.id, area_id=area.id, address="Тестовая, 1")
    other_house = House(
        organization_id=organization.id, area_id=other_area.id, address="Тестовая, 2"
    )
    db_session.add_all([house, other_house])
    await db_session.flush()
    entrance = Entrance(organization_id=organization.id, house_id=house.id, number="1")
    other_entrance = Entrance(organization_id=organization.id, house_id=other_house.id, number="1")
    employee = Employee(
        organization_id=organization.id,
        max_user_id=202,
        display_name="Тестовый оператор",
        role_id=role.id,
        area_id=area.id,
        all_houses=True,
        all_categories=False,
        category_ids=[str(category.id)],
    )
    db_session.add_all([entrance, other_entrance, employee])
    await db_session.commit()
    return Catalog(
        organization,
        area,
        other_area,
        house,
        other_house,
        entrance,
        other_entrance,
        category,
        other_category,
        initial,
        role,
        employee,
    )


def _payload(catalog: Catalog, **changes: Any) -> dict[str, Any]:
    return {
        "organization_id": str(catalog.organization.id),
        "house_id": str(catalog.house.id),
        "entrance_id": str(catalog.entrance.id),
        "category_id": str(catalog.category.id),
        "description": "Протекает труба",
    } | changes


async def _create(
    client: AsyncClient, headers: dict[str, str], catalog: Catalog, **changes: Any
) -> Response:
    return await client.post(
        "/api/requests",
        headers=headers | {"Idempotency-Key": str(uuid4())},
        json=_payload(catalog, **changes),
    )


async def test_manual_request_round_trip_has_initial_history_and_safe_audit(
    client: AsyncClient,
    db_session: AsyncSession,
    owner_headers: dict[str, str],
    catalog: Catalog,
) -> None:
    response = await _create(
        client,
        owner_headers,
        catalog,
        description="  Протекает труба  ",
        applicant_name="  Тестовый заявитель  ",
        applicant_phone="  +70000000000  ",
        apartment="  42  ",
        priority="high",
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["description"] == "Протекает труба"
    assert data["applicant_name"] == "Тестовый заявитель"
    assert data["applicant_phone"] == "+70000000000"
    assert data["apartment"] == "42"
    assert data["code"] == f"REQ-{data['number']:05d}"
    assert data["number"] > 0
    assert data["area_id"] == str(catalog.area.id)
    assert data["status_id"] == str(catalog.status.id)
    assert data["source"] == "manual"
    assert data["priority"] == "high"
    assert "payload_hash" not in data and "idempotency_key" not in data
    assert response.headers["Location"] == f"/api/requests/{data['id']}"
    assert response.headers["Cache-Control"] == "no-store"
    detail = await client.get(response.headers["Location"], headers=owner_headers)
    assert detail.status_code == 200 and detail.json() == data
    history = await client.get(response.headers["Location"] + "/history", headers=owner_headers)
    assert history.status_code == 200
    assert len(history.json()["items"]) == 1
    event_data = history.json()["items"][0]
    assert event_data["request_id"] == data["id"]
    assert event_data["from_status_id"] is None
    assert event_data["to_status_id"] == data["status_id"]
    assert event_data["actor_id"] == data["created_by"]
    audit = await db_session.scalar(select(AuditLog).where(AuditLog.action == "request.created"))
    assert audit is not None
    assert audit.actor_id == UUID(data["created_by"])
    assert audit.target_type == "requests" and audit.target_id == UUID(data["id"])


async def test_retries_normalize_payload_and_create_no_extra_history_or_audit(
    client: AsyncClient,
    db_session: AsyncSession,
    owner_headers: dict[str, str],
    catalog: Catalog,
) -> None:
    headers = owner_headers | {"Idempotency-Key": str(uuid4())}
    first = await client.post("/api/requests", headers=headers, json=_payload(catalog))
    replay = await client.post(
        "/api/requests",
        headers=headers,
        json=_payload(
            catalog, description="  Протекает труба  ", priority="normal", apartment=None
        ),
    )
    assert first.status_code == 201
    assert replay.status_code == 200 and replay.json() == first.json()
    assert await db_session.scalar(select(func.count()).select_from(ServiceRequest)) == 1
    assert await db_session.scalar(select(func.count()).select_from(RequestStatusHistory)) == 1
    assert (
        await db_session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "request.created")
        )
        == 1
    )
    conflict = await client.post(
        "/api/requests", headers=headers, json=_payload(catalog, description="Другой текст")
    )
    assert conflict.status_code == 409
    assert await db_session.scalar(select(func.count()).select_from(ServiceRequest)) == 1


async def test_idempotency_key_is_scoped_to_actor_and_organization(
    client: AsyncClient,
    db_session: AsyncSession,
    owner_headers: dict[str, str],
    employee_headers: dict[str, str],
    catalog: Catalog,
) -> None:
    other_catalog = await _build_catalog(db_session)
    key = {"Idempotency-Key": str(uuid4())}
    responses = [
        await client.post("/api/requests", headers=owner_headers | key, json=_payload(catalog)),
        await client.post("/api/requests", headers=employee_headers | key, json=_payload(catalog)),
        await client.post(
            "/api/requests", headers=owner_headers | key, json=_payload(other_catalog)
        ),
    ]
    assert all(response.status_code == 201 for response in responses)
    assert len({response.json()["id"] for response in responses}) == 3
    assert await db_session.scalar(select(func.count()).select_from(RequestStatusHistory)) == 3


@pytest.mark.parametrize(
    "changes",
    [
        {"description": "   "},
        {"description": "x" * 10001},
        {"priority": "urgent"},
        {"applicant_phone": "x" * 51},
        {"applicant_name": "x" * 201},
        {"apartment": "x" * 31},
        {"status_id": str(uuid4())},
        {"area_id": str(uuid4())},
        {"source": "ai"},
        {"created_by": str(uuid4())},
    ],
)
async def test_request_payload_validation_is_bounded_and_rejects_server_fields(
    client: AsyncClient, owner_headers: dict[str, str], catalog: Catalog, changes: dict[str, Any]
) -> None:
    response = await _create(client, owner_headers, catalog, **changes)
    assert response.status_code == 422


@pytest.mark.parametrize("key", [None, "not-a-uuid"])
async def test_idempotency_key_is_required_and_validated(
    client: AsyncClient, owner_headers: dict[str, str], catalog: Catalog, key: str | None
) -> None:
    headers = owner_headers if key is None else owner_headers | {"Idempotency-Key": key}
    response = await client.post("/api/requests", headers=headers, json=_payload(catalog))
    assert response.status_code == 422


@pytest.mark.parametrize("reference", ["house_id", "category_id", "entrance_id"])
async def test_missing_references_do_not_create_partial_requests(
    client: AsyncClient,
    db_session: AsyncSession,
    owner_headers: dict[str, str],
    catalog: Catalog,
    reference: str,
) -> None:
    response = await _create(client, owner_headers, catalog, **{reference: str(uuid4())})
    assert response.status_code == 422
    assert await db_session.scalar(select(func.count()).select_from(ServiceRequest)) == 0
    assert await db_session.scalar(select(func.count()).select_from(RequestStatusHistory)) == 0


async def test_entrance_must_belong_to_selected_house(
    client: AsyncClient, owner_headers: dict[str, str], catalog: Catalog
) -> None:
    response = await _create(
        client, owner_headers, catalog, entrance_id=str(catalog.other_entrance.id)
    )
    assert response.status_code == 422


async def test_foreign_category_is_rejected_even_for_owner(
    client: AsyncClient,
    db_session: AsyncSession,
    owner_headers: dict[str, str],
    catalog: Catalog,
) -> None:
    foreign = Organization(name="Synthetic foreign organization")
    db_session.add(foreign)
    await db_session.flush()
    category = Category(organization_id=foreign.id, name="Чужая категория")
    db_session.add(category)
    await db_session.commit()
    response = await _create(client, owner_headers, catalog, category_id=str(category.id))
    assert response.status_code == 422


async def test_initial_status_is_required_without_partial_writes(
    client: AsyncClient,
    db_session: AsyncSession,
    owner_headers: dict[str, str],
    catalog: Catalog,
) -> None:
    catalog.status.is_initial = False
    await db_session.commit()
    response = await _create(client, owner_headers, catalog)
    assert response.status_code == 409
    assert await db_session.scalar(select(func.count()).select_from(ServiceRequest)) == 0


async def test_history_failure_rolls_back_request_and_audit(
    client: AsyncClient,
    db_session: AsyncSession,
    owner_headers: dict[str, str],
    catalog: Catalog,
) -> None:
    def fail_history(session: Session, *_: object) -> None:
        if any(isinstance(row, RequestStatusHistory) for row in session.new):
            raise FlushError("Synthetic history write failure")

    event.listen(db_session.sync_session, "before_flush", fail_history)
    try:
        response = await _create(client, owner_headers, catalog)
        assert response.status_code == 503
        await db_session.rollback()
    finally:
        event.remove(db_session.sync_session, "before_flush", fail_history)
    assert await db_session.scalar(select(func.count()).select_from(ServiceRequest)) == 0
    assert await db_session.scalar(select(func.count()).select_from(RequestStatusHistory)) == 0
    assert (
        await db_session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "request.created")
        )
        == 0
    )


async def test_list_and_detail_enforce_intersection_of_area_and_category_scopes(
    client: AsyncClient,
    owner_headers: dict[str, str],
    employee_headers: dict[str, str],
    catalog: Catalog,
) -> None:
    allowed = await _create(client, owner_headers, catalog)
    wrong_category = await _create(
        client, owner_headers, catalog, category_id=str(catalog.other_category.id)
    )
    wrong_area = await _create(
        client, owner_headers, catalog, house_id=str(catalog.other_house.id), entrance_id=None
    )
    assert all(response.status_code == 201 for response in (allowed, wrong_category, wrong_area))
    query = {"organization_id": str(catalog.organization.id), "limit": 1, "offset": 0}
    page = await client.get("/api/requests", headers=employee_headers, params=query)
    assert page.status_code == 200
    assert [row["id"] for row in page.json()["items"]] == [allowed.json()["id"]]
    empty = await client.get(
        "/api/requests", headers=employee_headers, params=query | {"offset": 1}
    )
    assert empty.status_code == 200 and empty.json()["items"] == []
    for response in (wrong_category, wrong_area):
        for suffix in ("", "/history"):
            hidden = await client.get(
                f"/api/requests/{response.json()['id']}{suffix}", headers=employee_headers
            )
            assert hidden.status_code == 404
    assert (
        await client.get(f"/api/requests/{allowed.json()['id']}", headers=employee_headers)
    ).status_code == 200


async def test_create_rechecks_membership_permission_and_scope_on_replay(
    client: AsyncClient,
    db_session: AsyncSession,
    employee_headers: dict[str, str],
    catalog: Catalog,
) -> None:
    headers = employee_headers | {"Idempotency-Key": str(uuid4())}
    first = await client.post("/api/requests", headers=headers, json=_payload(catalog))
    assert first.status_code == 201
    catalog.employee.category_ids = []
    await db_session.commit()
    replay = await client.post("/api/requests", headers=headers, json=_payload(catalog))
    assert replay.status_code == 403
    assert (
        await client.get(f"/api/requests/{first.json()['id']}", headers=employee_headers)
    ).status_code == 404
    assert await db_session.scalar(select(func.count()).select_from(ServiceRequest)) == 1


@pytest.mark.parametrize("outside_scope", ["house", "category"])
async def test_creation_cannot_escape_employee_house_or_category_scope(
    client: AsyncClient,
    db_session: AsyncSession,
    employee_headers: dict[str, str],
    catalog: Catalog,
    outside_scope: str,
) -> None:
    changes = (
        {"house_id": str(catalog.other_house.id), "entrance_id": None}
        if outside_scope == "house"
        else {"category_id": str(catalog.other_category.id)}
    )
    response = await _create(client, employee_headers, catalog, **changes)
    assert response.status_code == 403
    assert await db_session.scalar(select(func.count()).select_from(ServiceRequest)) == 0


async def test_current_role_change_revokes_reads_and_creation(
    client: AsyncClient,
    db_session: AsyncSession,
    employee_headers: dict[str, str],
    catalog: Catalog,
) -> None:
    created = await _create(client, employee_headers, catalog)
    assert created.status_code == 201
    catalog.role.permissions = []
    await db_session.commit()
    assert (await _create(client, employee_headers, catalog)).status_code == 403
    page = await client.get(
        "/api/requests",
        headers=employee_headers,
        params={"organization_id": str(catalog.organization.id)},
    )
    assert page.status_code == 403
    for suffix in ("", "/history"):
        assert (
            await client.get(
                f"/api/requests/{created.json()['id']}{suffix}", headers=employee_headers
            )
        ).status_code == 404


async def test_environment_removal_blocks_idempotent_replay(
    client: AsyncClient,
    employee_headers: dict[str, str],
    catalog: Catalog,
    test_settings: Settings,
) -> None:
    headers = employee_headers | {"Idempotency-Key": str(uuid4())}
    assert (
        await client.post("/api/requests", headers=headers, json=_payload(catalog))
    ).status_code == 201
    test_settings.max_employee_ids = ()
    assert (
        await client.post("/api/requests", headers=headers, json=_payload(catalog))
    ).status_code == 401


async def test_filters_search_literal_text_code_and_inclusive_dates(
    client: AsyncClient,
    db_session: AsyncSession,
    owner_headers: dict[str, str],
    catalog: Catalog,
) -> None:
    first = await _create(client, owner_headers, catalog, description="Протечка 10%_трубы")
    second = await _create(
        client, owner_headers, catalog, description="Протечка трубы", priority="low"
    )
    assert first.status_code == second.status_code == 201
    record = await db_session.get(ServiceRequest, UUID(first.json()["id"]))
    assert record is not None
    record.created_at = datetime.now(UTC) - timedelta(days=2)
    await db_session.commit()
    base = {"organization_id": str(catalog.organization.id)}
    for query in (
        {"q": "%_"},
        {"q": first.json()["code"]},
        {
            "created_from": record.created_at.isoformat(),
            "created_to": record.created_at.isoformat(),
        },
    ):
        response = await client.get("/api/requests", headers=owner_headers, params=base | query)
        assert response.status_code == 200
        assert [item["id"] for item in response.json()["items"]] == [first.json()["id"]]
    filtered = await client.get(
        "/api/requests",
        headers=owner_headers,
        params=base
        | {
            "priority": "low",
            "house_id": str(catalog.house.id),
            "category_id": str(catalog.category.id),
            "status_id": str(catalog.status.id),
        },
    )
    assert filtered.status_code == 200
    assert [item["id"] for item in filtered.json()["items"]] == [second.json()["id"]]


@pytest.mark.parametrize(
    "query",
    [
        {"limit": "0"},
        {"limit": "101"},
        {"offset": "-1"},
        {"offset": "10001"},
        {"q": "x" * 201},
        {"priority": "urgent"},
        {"created_from": "2026-09-01T00:00:00"},
        {"created_from": "2026-09-02T00:00:00Z", "created_to": "2026-09-01T00:00:00Z"},
    ],
)
async def test_list_query_validation(
    client: AsyncClient, owner_headers: dict[str, str], catalog: Catalog, query: dict[str, str]
) -> None:
    response = await client.get(
        "/api/requests",
        headers=owner_headers,
        params={"organization_id": str(catalog.organization.id)} | query,
    )
    assert response.status_code == 422


async def test_unknown_and_foreign_resources_are_indistinguishable(
    client: AsyncClient,
    employee_headers: dict[str, str],
    owner_headers: dict[str, str],
    db_session: AsyncSession,
    catalog: Catalog,
) -> None:
    foreign = Organization(name="Synthetic foreign organization")
    db_session.add(foreign)
    await db_session.commit()
    for organization_id in (uuid4(), foreign.id):
        response = await client.get(
            "/api/requests",
            headers=employee_headers,
            params={"organization_id": str(organization_id)},
        )
        assert response.status_code == 404
    created = await _create(client, owner_headers, catalog)
    assert created.status_code == 201
    catalog.employee.is_active = False
    await db_session.commit()
    for request_id in (uuid4(), UUID(created.json()["id"])):
        for suffix in ("", "/history"):
            response = await client.get(
                f"/api/requests/{request_id}{suffix}", headers=employee_headers
            )
            assert response.status_code == 404


async def test_request_routes_require_authentication(client: AsyncClient, catalog: Catalog) -> None:
    responses = [
        await client.get("/api/requests", params={"organization_id": str(catalog.organization.id)}),
        await client.get(f"/api/requests/{uuid4()}"),
        await client.get(f"/api/requests/{uuid4()}/history"),
        await client.post(
            "/api/requests", json=_payload(catalog), headers={"Idempotency-Key": str(uuid4())}
        ),
    ]
    assert all(response.status_code == 401 for response in responses)


@pytest.mark.parametrize("conflicting_payload", [False, True])
async def test_concurrent_intake_creates_one_request_history_and_audit(
    test_settings: Settings, conflicting_payload: bool
) -> None:
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL must point to a disposable PostgreSQL database")
    # Separate committed connections are essential to exercise ON CONFLICT waiting.
    # This test owns an isolated schema left in the disposable database until its
    # container exits: no shared rows are deleted and no schema is dropped.
    schema_name = "crm_concurrency_" + uuid4().hex
    engine = create_async_engine(
        test_settings.database_url.get_secret_value(),
        hide_parameters=True,
        execution_options={"schema_translate_map": {None: schema_name}},
    )
    barrier = asyncio.Barrier(2)

    class RacingSession(AsyncSession):
        async def scalar(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(statement, Insert) and statement.table.name == "requests":
                await asyncio.wait_for(barrier.wait(), timeout=10)
            return await super().scalar(statement, *args, **kwargs)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    racing_factory = async_sessionmaker(engine, class_=RacingSession, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.execute(CreateSchema(schema_name))
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as setup:
            catalog = await _build_catalog(setup)
            user = User(max_user_id=101, display_name="Synthetic concurrent owner")
            setup.add(user)
            await setup.flush()
            session = AuthSession(
                user_id=user.id,
                token_hash="1" * 64,
                init_data_hash="2" * 64,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            setup.add(session)
            await setup.commit()
            authenticated = AuthenticatedSession(user=user, session=session, is_owner=True)
            key = uuid4()
            first_payload = RequestCreate.model_validate(_payload(catalog))
            second_payload = RequestCreate.model_validate(
                _payload(catalog, description="Другой текст")
                if conflicting_payload
                else _payload(catalog)
            )

        async def attempt(payload: RequestCreate) -> Any:
            async with racing_factory() as db:
                return await create_manual_request(db, authenticated, payload, key)

        results = await asyncio.wait_for(
            asyncio.gather(attempt(first_payload), attempt(second_payload), return_exceptions=True),
            timeout=20,
        )
        successes = [result for result in results if isinstance(result, tuple)]
        if conflicting_payload:
            assert len(successes) == 1 and successes[0][1] is True
            assert sum(isinstance(result, CRMConflict) for result in results) == 1
        else:
            assert len(successes) == 2, results
            assert sorted(result[1] for result in successes) == [False, True]
            assert successes[0][0] == successes[1][0]
        async with factory() as verify:
            assert await verify.scalar(select(func.count()).select_from(ServiceRequest)) == 1
            assert await verify.scalar(select(func.count()).select_from(RequestStatusHistory)) == 1
            assert await verify.scalar(select(func.count()).select_from(AuditLog)) == 1
    finally:
        await engine.dispose()
