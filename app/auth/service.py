"""Transactional MAX identity/session operations without HTTP dependencies."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import MaxIdentity, new_session_token, token_digest, valid_token_shape
from app.models.identity import AuditLog, AuthSession, User


class InvalidCredentials(Exception):
    """The identity or session cannot authorize this request."""


@dataclass(frozen=True)
class IssuedSession:
    user: User
    token: str = field(repr=False)
    expires_at: datetime
    is_owner: bool


@dataclass(frozen=True)
class AuthenticatedSession:
    user: User
    session: AuthSession
    is_owner: bool


async def login(
    db: AsyncSession,
    *,
    identity: MaxIdentity,
    ttl_seconds: int,
    owner_ids: tuple[int, ...],
    employee_ids: tuple[int, ...],
) -> IssuedSession:
    if identity.max_user_id not in (*owner_ids, *employee_ids):
        raise InvalidCredentials
    # Parallel first launches create one identity and one creation audit event.
    created_id = await db.scalar(
        insert(User)
        .values(
            max_user_id=identity.max_user_id, display_name=identity.display_name, is_active=True
        )
        .on_conflict_do_nothing(index_elements=[User.max_user_id])
        .returning(User.id)
    )
    user = await db.scalar(
        select(User).where(User.max_user_id == identity.max_user_id).with_for_update()
    )
    if user is None or not user.is_active:
        raise InvalidCredentials
    if created_id is not None:
        db.add(AuditLog(action="user.created", actor_id=user.id, subject_id=user.id))
    user.display_name = identity.display_name
    token = new_session_token()
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    created_session_id = await db.scalar(
        insert(AuthSession)
        .values(
            user_id=user.id,
            token_hash=token_digest(token),
            init_data_hash=token_digest(identity.signature),
            expires_at=expires_at,
        )
        .on_conflict_do_nothing(index_elements=[AuthSession.init_data_hash])
        .returning(AuthSession.id)
    )
    if created_session_id is None:
        await db.rollback()
        raise InvalidCredentials
    db.add(AuditLog(action="auth.login", actor_id=user.id, subject_id=user.id))
    await db.commit()
    return IssuedSession(
        user=user,
        token=token,
        expires_at=expires_at,
        is_owner=identity.max_user_id in owner_ids,
    )


async def authenticate(
    db: AsyncSession,
    token: str,
    *,
    owner_ids: tuple[int, ...],
    employee_ids: tuple[int, ...],
) -> AuthenticatedSession:
    if not valid_token_shape(token):
        raise InvalidCredentials
    result = await db.execute(
        select(AuthSession, User)
        .join(User, AuthSession.user_id == User.id)
        .where(
            AuthSession.token_hash == token_digest(token),
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > datetime.now(UTC),
            User.is_active.is_(True),
            User.max_user_id.in_((*owner_ids, *employee_ids)),
        )
    )
    row = result.one_or_none()
    if row is None:
        raise InvalidCredentials
    return AuthenticatedSession(
        user=row[1], session=row[0], is_owner=row[1].max_user_id in owner_ids
    )


async def logout(db: AsyncSession, authenticated: AuthenticatedSession) -> None:
    revoked = await db.scalar(
        update(AuthSession)
        .where(AuthSession.id == authenticated.session.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
        .returning(AuthSession.id)
    )
    if revoked is not None:
        db.add(
            AuditLog(
                action="auth.logout",
                actor_id=authenticated.user.id,
                subject_id=authenticated.user.id,
            )
        )
    await db.commit()


async def list_users(db: AsyncSession, *, limit: int, offset: int) -> list[User]:
    rows = await db.scalars(
        select(User).order_by(User.created_at, User.id).limit(limit).offset(offset)
    )
    return list(rows)
