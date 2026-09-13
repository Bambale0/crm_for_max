"""Owner-managed staff and bot settings for the native MAX bot."""

from collections.abc import Collection
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.actor import ActorContext
from app.crm.access import load_access
from app.crm.errors import CRMConflict, CRMInvalidReference, CRMNotFound, CRMPermissionDenied
from app.models.bot import BotGroupChat, BotOrganizationSettings
from app.models.crm import Employee, Role
from app.models.identity import AuditLog

EXECUTOR_ROLE_NAME = "Сотрудник бота"
OPERATOR_ROLE_NAME = "Оператор"
ROLE_KEYS = {
    "executor": EXECUTOR_ROLE_NAME,
    "operator": OPERATOR_ROLE_NAME,
}


def require_owner(actor: ActorContext) -> None:
    if not actor.is_owner:
        raise CRMPermissionDenied


async def role_for_key(
    db: AsyncSession,
    organization_id: UUID,
    role_key: str,
) -> Role:
    role_name = ROLE_KEYS.get(role_key)
    if role_name is None:
        raise CRMInvalidReference
    role = await db.scalar(
        select(Role).where(
            Role.organization_id == organization_id,
            Role.name == role_name,
        )
    )
    if role is None:
        raise CRMNotFound
    return role


async def list_staff(
    db: AsyncSession,
    organization_id: UUID,
    *,
    offset: int,
    limit: int,
) -> list[tuple[Employee, Role]]:
    if not 0 <= offset <= 10_000 or not 1 <= limit <= 100:
        raise CRMInvalidReference
    return list(
        (
            await db.execute(
                select(Employee, Role)
                .join(
                    Role,
                    (Role.id == Employee.role_id)
                    & (Role.organization_id == Employee.organization_id),
                )
                .where(Employee.organization_id == organization_id)
                .order_by(Employee.is_active.desc(), Employee.display_name, Employee.id)
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )


async def get_staff(
    db: AsyncSession,
    organization_id: UUID,
    employee_id: UUID,
) -> tuple[Employee, Role]:
    row = (
        await db.execute(
            select(Employee, Role)
            .join(
                Role,
                (Role.id == Employee.role_id)
                & (Role.organization_id == Employee.organization_id),
            )
            .where(
                Employee.id == employee_id,
                Employee.organization_id == organization_id,
            )
        )
    ).one_or_none()
    if row is None:
        raise CRMNotFound
    return row[0], row[1]


async def add_staff(
    db: AsyncSession,
    actor: ActorContext,
    organization_id: UUID,
    max_user_id: int,
    role_key: str,
) -> Employee:
    require_owner(actor)
    if not 0 < max_user_id < 2**63:
        raise CRMInvalidReference
    role = await role_for_key(db, organization_id, role_key)
    existing = await db.scalar(
        select(Employee).where(
            Employee.organization_id == organization_id,
            Employee.max_user_id == max_user_id,
        )
    )
    if existing is not None:
        raise CRMConflict
    employee = Employee(
        organization_id=organization_id,
        max_user_id=max_user_id,
        display_name=f"MAX {max_user_id}",
        role_id=role.id,
        all_houses=True,
        all_categories=True,
    )
    db.add(employee)
    await db.flush()
    db.add(
        AuditLog(
            actor_id=actor.user.id,
            action="employee.created",
            target_type="employees",
            target_id=employee.id,
        )
    )
    return employee


async def set_staff_role(
    db: AsyncSession,
    actor: ActorContext,
    organization_id: UUID,
    employee_id: UUID,
    role_key: str,
    protected_max_ids: Collection[int],
) -> Employee:
    require_owner(actor)
    employee, _ = await get_staff(db, organization_id, employee_id)
    if employee.max_user_id in protected_max_ids:
        raise CRMPermissionDenied
    role = await role_for_key(db, organization_id, role_key)
    if employee.role_id != role.id:
        employee.role_id = role.id
        db.add(
            AuditLog(
                actor_id=actor.user.id,
                action="employee.role_changed",
                target_type="employees",
                target_id=employee.id,
            )
        )
    await db.flush()
    return employee


async def set_staff_active(
    db: AsyncSession,
    actor: ActorContext,
    organization_id: UUID,
    employee_id: UUID,
    is_active: bool,
    protected_max_ids: Collection[int],
) -> Employee:
    require_owner(actor)
    employee, _ = await get_staff(db, organization_id, employee_id)
    if employee.max_user_id in protected_max_ids:
        raise CRMPermissionDenied
    if employee.is_active != is_active:
        employee.is_active = is_active
        db.add(
            AuditLog(
                actor_id=actor.user.id,
                action="employee.activated" if is_active else "employee.deactivated",
                target_type="employees",
                target_id=employee.id,
            )
        )
    await db.flush()
    return employee


async def staff_is_dispatcher(
    db: AsyncSession,
    actor: ActorContext,
    organization_id: UUID,
) -> bool:
    if actor.is_owner:
        return True
    access = await load_access(db, actor, organization_id, "requests.view")
    return "requests.assign" in access.permissions


async def dispatcher_max_ids(
    db: AsyncSession,
    organization_id: UUID,
    owner_ids: Collection[int],
) -> tuple[int, ...]:
    rows = (
        await db.execute(
            select(Employee, Role)
            .join(
                Role,
                (Role.id == Employee.role_id)
                & (Role.organization_id == Employee.organization_id),
            )
            .where(
                Employee.organization_id == organization_id,
                Employee.is_active.is_(True),
            )
        )
    ).all()
    result = list(owner_ids)
    result.extend(
        employee.max_user_id
        for employee, role in rows
        if "requests.assign" in role.permissions
    )
    return tuple(dict.fromkeys(result))


async def ensure_group_chat(
    db: AsyncSession,
    organization_id: UUID,
    chat_id: int,
) -> BotGroupChat:
    if not -(2**63) <= chat_id < 2**63 or chat_id == 0:
        raise CRMInvalidReference
    await db.execute(
        insert(BotGroupChat)
        .values(chat_id=chat_id, organization_id=organization_id)
        .on_conflict_do_update(
            index_elements=[BotGroupChat.chat_id],
            set_={"last_seen_at": func.now()},
        )
    )
    chat = await db.get(BotGroupChat, chat_id, populate_existing=True)
    if chat is None or chat.organization_id != organization_id:
        raise CRMConflict
    return chat


async def get_bot_settings(
    db: AsyncSession,
    organization_id: UUID,
) -> BotOrganizationSettings:
    await db.execute(
        insert(BotOrganizationSettings)
        .values(organization_id=organization_id)
        .on_conflict_do_nothing(index_elements=[BotOrganizationSettings.organization_id])
    )
    settings = await db.get(BotOrganizationSettings, organization_id, populate_existing=True)
    if settings is None:
        raise CRMConflict
    return settings


async def list_group_chats(
    db: AsyncSession,
    organization_id: UUID,
    *,
    offset: int,
    limit: int,
) -> list[BotGroupChat]:
    if not 0 <= offset <= 10_000 or not 1 <= limit <= 100:
        raise CRMInvalidReference
    return list(
        await db.scalars(
            select(BotGroupChat)
            .where(BotGroupChat.organization_id == organization_id)
            .order_by(BotGroupChat.last_seen_at.desc(), BotGroupChat.chat_id)
            .offset(offset)
            .limit(limit)
        )
    )


async def set_group_chat_analysis(
    db: AsyncSession,
    actor: ActorContext,
    organization_id: UUID,
    chat_id: int,
    enabled: bool,
) -> BotGroupChat:
    require_owner(actor)
    chat = await db.get(BotGroupChat, chat_id, populate_existing=True)
    if chat is None or chat.organization_id != organization_id:
        raise CRMNotFound
    if chat.analysis_enabled != enabled:
        chat.analysis_enabled = enabled
        db.add(
            AuditLog(
                actor_id=actor.user.id,
                action="bot.chat_analysis_enabled" if enabled else "bot.chat_analysis_disabled",
                target_type="bot_group_chats",
            )
        )
    await db.flush()
    return chat


async def set_global_group_analysis(
    db: AsyncSession,
    actor: ActorContext,
    organization_id: UUID,
    enabled: bool,
) -> BotOrganizationSettings:
    require_owner(actor)
    settings = await get_bot_settings(db, organization_id)
    if settings.group_analysis_enabled != enabled:
        settings.group_analysis_enabled = enabled
        db.add(
            AuditLog(
                actor_id=actor.user.id,
                action=(
                    "bot.group_analysis_enabled"
                    if enabled
                    else "bot.group_analysis_disabled"
                ),
                target_type="bot_settings",
                target_id=organization_id,
            )
        )
    await db.flush()
    return settings
