from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.crm import Employee, House, RequestStatus
from app.models.identity import AuditLog

pytestmark = pytest.mark.integration


async def create(
    client: AsyncClient, headers: dict[str, str], path: str, payload: dict[str, Any]
) -> dict[str, Any]:
    response = await client.post("/api/" + path, headers=headers, json=payload)
    assert response.status_code == 201, response.text
    assert response.headers["Cache-Control"] == "no-store"
    return response.json()


@pytest.fixture
async def catalog(client: AsyncClient, owner_headers: dict[str, str]) -> dict[str, Any]:
    org = await create(client, owner_headers, "organizations", {"name": "Synthetic УК"})
    common = {"organization_id": org["id"]}
    district = await create(client, owner_headers, "districts", {**common, "name": "Район"})
    area = await create(
        client, owner_headers, "areas", {**common, "district_id": district["id"], "name": "Участок"}
    )
    house = await create(
        client,
        owner_headers,
        "houses",
        {**common, "area_id": area["id"], "address": "Тестовая, 18"},
    )
    entrance = await create(
        client, owner_headers, "entrances", {**common, "house_id": house["id"], "number": "3"}
    )
    category = await create(client, owner_headers, "categories", {**common, "name": "Электрика"})
    role = await create(
        client,
        owner_headers,
        "roles",
        {
            **common,
            "name": "Диспетчер",
            "permissions": ["houses.view", "employees.view", "requests.view", "requests.create"],
        },
    )
    employee = await create(
        client,
        owner_headers,
        "employees",
        {
            **common,
            "max_user_id": 202,
            "display_name": "Тестовый сотрудник",
            "role_id": role["id"],
            "area_id": area["id"],
            "house_ids": [house["id"]],
            "category_ids": [category["id"]],
        },
    )
    return {
        "org": org,
        "district": district,
        "area": area,
        "house": house,
        "entrance": entrance,
        "category": category,
        "role": role,
        "employee": employee,
    }


@pytest.mark.parametrize(
    "resource", ["districts", "areas", "houses", "entrances", "categories", "employees"]
)
async def test_operator_reads_only_configured_catalog(
    client: AsyncClient, catalog: dict[str, Any], employee_headers: dict[str, str], resource: str
) -> None:
    response = await client.get(
        "/api/" + resource,
        params={"organization_id": catalog["org"]["id"]},
        headers=employee_headers,
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 1


async def test_organization_discovery_never_exposes_other_memberships(
    client: AsyncClient,
    owner_headers: dict[str, str],
    employee_headers: dict[str, str],
    catalog: dict[str, Any],
) -> None:
    foreign = await create(client, owner_headers, "organizations", {"name": "Другая УК"})
    visible = await client.get("/api/organizations", headers=employee_headers)
    assert [row["id"] for row in visible.json()["items"]] == [catalog["org"]["id"]]
    denied = await client.get(
        "/api/houses", params={"organization_id": foreign["id"]}, headers=employee_headers
    )
    assert denied.status_code == 404


async def test_scope_filters_apply_before_pagination_and_search(
    client: AsyncClient,
    owner_headers: dict[str, str],
    employee_headers: dict[str, str],
    catalog: dict[str, Any],
) -> None:
    common = {"organization_id": catalog["org"]["id"]}
    await create(
        client,
        owner_headers,
        "houses",
        {**common, "area_id": catalog["area"]["id"], "address": "AAA недоступный"},
    )
    await create(client, owner_headers, "categories", {**common, "name": "AAA недоступная"})
    for resource, expected in (
        ("houses", catalog["house"]["id"]),
        ("categories", catalog["category"]["id"]),
    ):
        response = await client.get(
            "/api/" + resource, params={**common, "limit": 1}, headers=employee_headers
        )
        assert [item["id"] for item in response.json()["items"]] == [expected]
    response = await client.get(
        "/api/houses", params={**common, "q": "%"}, headers=employee_headers
    )
    assert response.json()["items"] == []


async def test_employee_scope_change_and_deactivation_apply_to_current_session(
    client: AsyncClient,
    owner_headers: dict[str, str],
    employee_headers: dict[str, str],
    catalog: dict[str, Any],
) -> None:
    common = {"organization_id": catalog["org"]["id"]}
    path = "/api/employees/" + catalog["employee"]["id"]
    empty = await client.patch(path, params=common, headers=owner_headers, json={"house_ids": []})
    assert empty.status_code == 200, empty.text
    assert (await client.get("/api/houses", params=common, headers=employee_headers)).json()[
        "items"
    ] == []
    all_houses = await client.patch(
        path, params=common, headers=owner_headers, json={"all_houses": True}
    )
    assert all_houses.status_code == 200
    assert (
        len(
            (await client.get("/api/houses", params=common, headers=employee_headers)).json()[
                "items"
            ]
        )
        == 1
    )
    assert (
        await client.patch(path, params=common, headers=owner_headers, json={"is_active": False})
    ).status_code == 200
    assert (
        await client.get("/api/houses", params=common, headers=employee_headers)
    ).status_code == 404


async def test_role_update_revokes_permission_and_ordinary_employee_cannot_edit(
    client: AsyncClient,
    owner_headers: dict[str, str],
    employee_headers: dict[str, str],
    catalog: dict[str, Any],
) -> None:
    common = {"organization_id": catalog["org"]["id"]}
    path = "/api/roles/" + catalog["role"]["id"]
    assert (
        await client.patch(path, params=common, headers=employee_headers, json={"permissions": []})
    ).status_code == 403
    response = await client.patch(
        path, params=common, headers=owner_headers, json={"permissions": ["requests.view"]}
    )
    assert response.status_code == 200
    assert (
        await client.get("/api/houses", params=common, headers=employee_headers)
    ).status_code == 403
    assert (
        await client.get("/api/categories", params=common, headers=employee_headers)
    ).status_code == 200


async def test_cross_organization_and_area_references_do_not_write(
    client: AsyncClient,
    owner_headers: dict[str, str],
    catalog: dict[str, Any],
    db_session: AsyncSession,
) -> None:
    foreign = await create(client, owner_headers, "organizations", {"name": "Чужая УК"})
    response = await client.post(
        "/api/houses",
        headers=owner_headers,
        json={
            "organization_id": foreign["id"],
            "area_id": catalog["area"]["id"],
            "address": "Запрещённый",
        },
    )
    assert response.status_code == 422
    assert await db_session.scalar(select(func.count()).select_from(House)) == 1
    common = {"organization_id": catalog["org"]["id"]}
    area = await create(
        client,
        owner_headers,
        "areas",
        {**common, "district_id": catalog["district"]["id"], "name": "Другой участок"},
    )
    response = await client.patch(
        "/api/employees/" + catalog["employee"]["id"],
        params=common,
        headers=owner_headers,
        json={"area_id": area["id"]},
    )
    assert response.status_code == 422
    employee = await db_session.get(
        Employee, UUID(catalog["employee"]["id"]), populate_existing=True
    )
    assert employee is not None and str(employee.area_id) == catalog["area"]["id"]


async def test_duplicate_employee_and_initial_status_are_conflicts(
    client: AsyncClient,
    owner_headers: dict[str, str],
    catalog: dict[str, Any],
    db_session: AsyncSession,
) -> None:
    common = {"organization_id": catalog["org"]["id"]}
    duplicate = await client.post(
        "/api/employees",
        headers=owner_headers,
        json={
            **common,
            "max_user_id": 202,
            "display_name": "Дубликат",
            "role_id": catalog["role"]["id"],
        },
    )
    assert duplicate.status_code == 409
    custom = await create(
        client, owner_headers, "statuses", {**common, "name": "Осмотр", "code": "inspection"}
    )
    assert custom["is_initial"] is False
    initial = await client.post(
        "/api/statuses",
        headers=owner_headers,
        json={**common, "name": "Вторая новая", "code": "another_new", "is_initial": True},
    )
    assert initial.status_code == 409
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(RequestStatus)
            .where(RequestStatus.is_initial.is_(True))
        )
        == 1
    )


@pytest.mark.parametrize("payload", [{"name": "  "}, {"name": "Name", "is_owner": True}])
async def test_catalog_input_rejects_blank_or_extra_fields(
    client: AsyncClient, owner_headers: dict[str, str], payload: dict[str, Any]
) -> None:
    assert (
        await client.post("/api/organizations", headers=owner_headers, json=payload)
    ).status_code == 422


async def test_unsupported_role_permission_is_rejected(
    client: AsyncClient, owner_headers: dict[str, str], catalog: dict[str, Any]
) -> None:
    response = await client.post(
        "/api/roles",
        headers=owner_headers,
        json={
            "organization_id": catalog["org"]["id"],
            "name": "Неверная роль",
            "permissions": ["system.owner"],
        },
    )
    assert response.status_code == 422


async def test_catalog_audit_identifies_actor_and_resource_without_payload(
    catalog: dict[str, Any], db_session: AsyncSession
) -> None:
    events = list(
        await db_session.scalars(select(AuditLog).where(AuditLog.action == "catalog.create"))
    )
    assert len(events) == 8
    assert all(event.actor_id is not None and event.target_id is not None for event in events)
    assert {event.target_type for event in events} == {
        "organizations",
        "districts",
        "areas",
        "houses",
        "entrances",
        "categories",
        "roles",
        "employees",
    }
