"""HTTP adapters for MAX Mini App authentication and environment access rules."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_redis, get_settings
from app.auth import service
from app.auth.rate_limit import (
    InitDataReplayed,
    LoginRateLimited,
    RateLimiterUnavailable,
    check_login_budget,
    consume_init_data,
)
from app.auth.schemas import LoginRequest, LoginResponse, UserRead, UsersPage
from app.auth.security import InvalidInitData, verify_max_init_data
from app.core.config import Settings
from app.models.identity import User

router = APIRouter(prefix="/api", tags=["authentication"])
_bearer = HTTPBearer(auto_error=False)
DatabaseDependency = Annotated[AsyncSession, Depends(get_db)]
RedisDependency = Annotated[Redis, Depends(get_redis)]
SettingsDependency = Annotated[Settings, Depends(get_settings)]


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials or session",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Authentication temporarily unavailable",
    )


def _user_view(user: User, *, is_owner: bool) -> UserRead:
    return UserRead(
        id=user.id,
        max_user_id=user.max_user_id,
        display_name=user.display_name,
        is_active=user.is_active,
        is_owner=is_owner,
        created_at=user.created_at,
    )


async def get_authenticated_session(
    db: DatabaseDependency,
    settings: SettingsDependency,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> service.AuthenticatedSession:
    if credentials is None:
        raise _unauthorized()
    if settings.max_staff_token is None:
        raise _unavailable()
    try:
        return await service.authenticate(
            db,
            credentials.credentials,
            owner_ids=settings.max_owner_ids,
            employee_ids=settings.max_employee_ids,
        )
    except service.InvalidCredentials:
        raise _unauthorized() from None


SessionDependency = Annotated[service.AuthenticatedSession, Depends(get_authenticated_session)]


async def require_owner(authenticated: SessionDependency) -> service.AuthenticatedSession:
    if not authenticated.is_owner:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner access required")
    return authenticated


@router.post("/auth/max", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: DatabaseDependency,
    redis: RedisDependency,
    settings: SettingsDependency,
) -> LoginResponse:
    if settings.max_staff_token is None or not (
        settings.max_owner_ids or settings.max_employee_ids
    ):
        raise _unavailable()
    try:
        await check_login_budget(
            redis,
            scope="source",
            identifier=request.client.host if request.client else "unknown",
            limit=settings.login_rate_limit,
            window_seconds=settings.login_rate_window_seconds,
        )
        identity = verify_max_init_data(
            payload.init_data.get_secret_value(),
            bot_token=settings.max_staff_token.get_secret_value(),
            ttl_seconds=settings.max_init_data_ttl_seconds,
            future_skew_seconds=settings.max_init_data_future_skew_seconds,
        )
        if identity.max_user_id not in (*settings.max_owner_ids, *settings.max_employee_ids):
            raise _unauthorized()
        await check_login_budget(
            redis,
            scope="account",
            identifier=str(identity.max_user_id),
            limit=settings.login_rate_limit,
            window_seconds=settings.login_rate_window_seconds,
        )
        await consume_init_data(
            redis,
            signature=identity.signature,
            ttl_seconds=(
                settings.max_init_data_ttl_seconds + settings.max_init_data_future_skew_seconds + 1
            ),
        )
        issued = await service.login(
            db,
            identity=identity,
            ttl_seconds=settings.session_ttl_seconds,
            owner_ids=settings.max_owner_ids,
            employee_ids=settings.max_employee_ids,
        )
    except LoginRateLimited as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts",
            headers={"Retry-After": str(error.retry_after)},
        ) from None
    except RateLimiterUnavailable:
        raise _unavailable() from None
    except (InvalidInitData, InitDataReplayed, service.InvalidCredentials):
        raise _unauthorized() from None
    response.headers["Cache-Control"] = "no-store"
    return LoginResponse(
        access_token=issued.token,
        expires_at=issued.expires_at,
        user=_user_view(issued.user, is_owner=issued.is_owner),
    )


@router.get("/auth/me", response_model=UserRead)
async def current_user(authenticated: SessionDependency, response: Response) -> UserRead:
    response.headers["Cache-Control"] = "no-store"
    return _user_view(authenticated.user, is_owner=authenticated.is_owner)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(db: DatabaseDependency, authenticated: SessionDependency) -> Response:
    await service.logout(db, authenticated)
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": "no-store"})


@router.get("/admin/users", response_model=UsersPage, tags=["administration"])
async def users(
    db: DatabaseDependency,
    owner: Annotated[service.AuthenticatedSession, Depends(require_owner)],
    settings: SettingsDependency,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
) -> UsersPage:
    rows = await service.list_users(db, limit=limit, offset=offset)
    response.headers["Cache-Control"] = "no-store"
    return UsersPage(
        items=[
            _user_view(user, is_owner=user.max_user_id in settings.max_owner_ids) for user in rows
        ],
        limit=limit,
        offset=offset,
    )
