"""Env-authorized provider identities, independent of browser sessions."""

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.actor import Actor, ActorContext
from app.auth.service import InvalidCredentials
from app.core.config import Settings
from app.models.crm import Employee, Role
from app.models.identity import AuditLog, User


async def native_actor(
    db: AsyncSession, settings: Settings, max_user_id: int, display_name: str | None = None
) -> Actor:
    if max_user_id not in (*settings.max_owner_ids, *settings.max_employee_ids):
        raise InvalidCredentials
    if display_name is not None:
        created = await db.scalar(
            insert(User)
            .values(max_user_id=max_user_id, display_name=display_name)
            .on_conflict_do_nothing(index_elements=[User.max_user_id])
            .returning(User.id)
        )
        if created is not None:
            db.add(AuditLog(action="user.created", actor_id=created, subject_id=created))
    user = await db.scalar(
        select(User)
        .where(User.max_user_id == max_user_id)
        .execution_options(populate_existing=True)
    )
    if user is None or not user.is_active:
        raise InvalidCredentials
    if display_name is not None:
        user.display_name = display_name
        employees = await db.scalars(
            select(Employee).where(
                Employee.max_user_id == max_user_id,
                Employee.display_name == f"MAX {max_user_id}",
            )
        )
        for employee in employees:
            employee.display_name = display_name
    return Actor(user=user, is_owner=max_user_id in settings.max_owner_ids)


async def access_stamp(db: AsyncSession, actor: ActorContext) -> str:
    """Discard queued private content when membership, role or scopes changed."""
    scopes: list[object] = [actor.is_owner]
    if not actor.is_owner:
        rows = await db.execute(
            select(Employee, Role)
            .join(Role, Role.id == Employee.role_id)
            .where(Employee.max_user_id == actor.user.max_user_id)
            .order_by(Employee.id)
            .execution_options(populate_existing=True)
        )
        for employee, role in rows:
            scopes.append(
                [
                    str(employee.organization_id),
                    employee.is_active,
                    str(employee.area_id),
                    employee.all_houses,
                    employee.all_categories,
                    employee.house_ids,
                    employee.category_ids,
                    role.permissions,
                ]
            )
    return hashlib.sha256(json.dumps(scopes, sort_keys=True).encode()).hexdigest()
