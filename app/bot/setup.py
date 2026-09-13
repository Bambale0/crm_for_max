"""One-time setup of the small bot catalog, without a web administration UI."""

import argparse
import asyncio
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import Database
from app.models.bot import HouseChat
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

EMPLOYEE_PERMISSIONS = ["houses.view", "requests.view", "requests.create", "requests.update"]
OPERATOR_PERMISSIONS = [
    "houses.view",
    "employees.view",
    "requests.view",
    "requests.create",
    "requests.assign",
    "requests.update",
]


async def initialize(
    db: AsyncSession,
    settings: Settings,
    name: str,
    houses: list[str],
    organization_id: UUID | None = None,
) -> Organization:
    if not settings.max_owner_ids:
        raise ValueError("Set MAX_OWNER_IDS before initializing the bot")
    if (
        not name.strip()
        or len(name.strip()) > 200
        or not houses
        or any(not h.strip() or len(h.strip()) > 500 for h in houses)
    ):
        raise ValueError("Provide an organization name and at least one house address")
    if organization_id:
        org = await db.get(Organization, organization_id)
        if org is None:
            raise ValueError("Organization does not exist")
    else:
        if await db.scalar(select(Organization.id).limit(1)):
            raise ValueError("Catalog already exists; provide --organization-id to extend it")
        org = Organization(name=name.strip())
        db.add(org)
        await db.flush()
    district = await db.scalar(
        select(District).where(District.organization_id == org.id).order_by(District.id).limit(1)
    )
    if district is None:
        district = District(organization_id=org.id, name="Основной")
        db.add(district)
        await db.flush()
    area = await db.scalar(
        select(Area)
        .where(Area.organization_id == org.id, Area.district_id == district.id)
        .order_by(Area.id)
        .limit(1)
    )
    if area is None:
        area = Area(organization_id=org.id, district_id=district.id, name="Основной")
        db.add(area)
        await db.flush()
    for address in dict.fromkeys(h.strip() for h in houses):
        existing = await db.scalar(
            select(House.id).where(House.organization_id == org.id, House.address == address)
        )
        if existing is None:
            db.add(House(organization_id=org.id, area_id=area.id, address=address))
    if not await db.scalar(select(Category.id).where(Category.organization_id == org.id).limit(1)):
        db.add(Category(organization_id=org.id, name="Общее"))
    if not await db.scalar(
        select(RequestStatus.id).where(
            RequestStatus.organization_id == org.id, RequestStatus.is_initial.is_(True)
        )
    ):
        if await db.scalar(
            select(RequestStatus.id).where(
                RequestStatus.organization_id == org.id, RequestStatus.code == "new"
            )
        ):
            raise ValueError("Existing new status is not initial; configure the catalog first")
        db.add(RequestStatus(organization_id=org.id, name="Новая", code="new", is_initial=True))
    employee_role = await db.scalar(
        select(Role)
        .where(Role.organization_id == org.id, Role.name == "Сотрудник бота")
        .order_by(Role.id)
        .limit(1)
    )
    if employee_role is None:
        employee_role = Role(
            organization_id=org.id,
            name="Сотрудник бота",
            permissions=EMPLOYEE_PERMISSIONS,
        )
        db.add(employee_role)
        await db.flush()

    operator_role = await db.scalar(
        select(Role)
        .where(Role.organization_id == org.id, Role.name == "Оператор")
        .order_by(Role.id)
        .limit(1)
    )
    if operator_role is None:
        operator_role = Role(
            organization_id=org.id,
            name="Оператор",
            permissions=OPERATOR_PERMISSIONS,
        )
        db.add(operator_role)
        await db.flush()

    for max_id in settings.max_staff_ids:
        employee = await db.scalar(
            select(Employee.id).where(
                Employee.organization_id == org.id, Employee.max_user_id == max_id
            )
        )
        if employee is not None:
            continue
        target_role = operator_role if max_id in settings.max_dispatcher_ids else employee_role
        db.add(
            Employee(
                organization_id=org.id,
                max_user_id=max_id,
                display_name=f"MAX {max_id}",
                role_id=target_role.id,
                all_houses=True,
                all_categories=True,
            )
        )
    await db.flush()
    return org


async def bind_chat(db: AsyncSession, chat_id: int, house_id: UUID, category_id: UUID) -> None:
    if not -(2**63) <= chat_id < 2**63 or chat_id == 0:
        raise ValueError("Chat ID must be a nonzero signed 64-bit integer")
    house, category = await db.get(House, house_id), await db.get(Category, category_id)
    if house is None or category is None or house.organization_id != category.organization_id:
        raise ValueError("House and category must exist in the same organization")
    existing = await db.get(HouseChat, chat_id)
    if existing:
        if existing.house_id != house_id or existing.category_id != category_id:
            raise ValueError(
                "Chat already bound; rebinding would change the meaning of saved observations"
            )
    else:
        db.add(
            HouseChat(
                chat_id=chat_id,
                organization_id=house.organization_id,
                house_id=house_id,
                category_id=category_id,
            )
        )
    await db.flush()


async def run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--name", required=True)
    init.add_argument("--house", action="append", required=True)
    init.add_argument("--organization-id", type=UUID)
    bind = commands.add_parser("bind-chat")
    bind.add_argument("--chat-id", type=int, required=True)
    bind.add_argument("--house-id", type=UUID, required=True)
    bind.add_argument("--category-id", type=UUID, required=True)
    args = parser.parse_args()
    settings = Settings()
    database = Database(settings.database_url.get_secret_value())
    try:
        async with database.session_factory() as db:
            if args.command == "init":
                org = await initialize(db, settings, args.name, args.house, args.organization_id)
                await db.commit()
                print(f"MAX_BOT_ORGANIZATION_ID={org.id}")
                for house in await db.scalars(select(House).where(House.organization_id == org.id)):
                    print(f"house {house.id}: {house.address}")
                for category in await db.scalars(
                    select(Category).where(Category.organization_id == org.id)
                ):
                    print(f"category {category.id}: {category.name}")
            else:
                await bind_chat(db, args.chat_id, args.house_id, args.category_id)
                await db.commit()
                print("Chat binding saved")
    finally:
        await database.close()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except ValueError as error:
        raise SystemExit(str(error)) from None
