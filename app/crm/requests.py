"""Manual request intake and reads; creation and its initial history are atomic."""

import hashlib
import json
import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response, status
from sqlalchemy import Select, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.actor import ActorContext
from app.auth.routes import DatabaseDependency, SessionDependency
from app.crm.access import CRMAccess, load_access
from app.crm.errors import CRMConflict, CRMInvalidReference, CRMNotFound, CRMPermissionDenied
from app.crm.request_schemas import (
    RequestCreate,
    RequestFilters,
    RequestHistoryPage,
    RequestHistoryRead,
    RequestPage,
    RequestRead,
)
from app.models.crm import (
    Category,
    Entrance,
    House,
    RequestStatus,
    RequestStatusHistory,
    ServiceRequest,
)
from app.models.identity import AuditLog

router = APIRouter(prefix="/api/requests", tags=["requests"])


def _payload_hash(payload: RequestCreate) -> str:
    normalized = json.dumps(
        payload.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(normalized.encode()).hexdigest()


def _view(request: ServiceRequest, area_id: UUID) -> RequestRead:
    return RequestRead.model_validate(
        {
            name: getattr(request, name)
            for name in RequestRead.model_fields
            if name not in {"code", "area_id"}
        }
        | {"code": f"REQ-{request.number:05d}", "area_id": area_id}
    )


def _visible_requests(access: CRMAccess) -> Select[tuple[ServiceRequest, UUID]]:
    return (
        select(ServiceRequest, House.area_id)
        .join(House, ServiceRequest.house_id == House.id)
        .where(access.house_predicate(), access.category_predicate())
    )


async def _visible_request(
    db: AsyncSession, authenticated: ActorContext, request_id: UUID
) -> tuple[ServiceRequest, UUID]:
    organization_id = await db.scalar(
        select(ServiceRequest.organization_id).where(ServiceRequest.id == request_id)
    )
    if organization_id is None:
        raise CRMNotFound
    try:
        access = await load_access(db, authenticated, organization_id, "requests.view")
    except CRMPermissionDenied:
        # Card and history lookup never disclose another employee's request.
        raise CRMNotFound from None
    row = (
        await db.execute(_visible_requests(access).where(ServiceRequest.id == request_id))
    ).one_or_none()
    if row is None:
        raise CRMNotFound
    return row[0], row[1]


async def create_manual_request(
    db: AsyncSession,
    authenticated: ActorContext,
    payload: RequestCreate,
    idempotency_key: UUID,
    *,
    commit: bool = True,
) -> tuple[RequestRead, bool]:
    """Revalidate current scopes even for retries, then persist one request/history pair."""
    access = await load_access(db, authenticated, payload.organization_id, "requests.create")
    house = await db.scalar(
        select(House).where(
            House.id == payload.house_id, House.organization_id == payload.organization_id
        )
    )
    category = await db.scalar(
        select(Category).where(
            Category.id == payload.category_id, Category.organization_id == payload.organization_id
        )
    )
    if house is None or category is None:
        raise CRMInvalidReference
    if not access.can_house(house) or not access.can_category(category.id):
        raise CRMPermissionDenied
    if payload.entrance_id is not None:
        entrance = await db.scalar(
            select(Entrance.id).where(
                Entrance.id == payload.entrance_id,
                Entrance.organization_id == payload.organization_id,
                Entrance.house_id == house.id,
            )
        )
        if entrance is None:
            raise CRMInvalidReference

    digest = _payload_hash(payload)
    retry_query = select(ServiceRequest).where(
        ServiceRequest.organization_id == payload.organization_id,
        ServiceRequest.created_by == authenticated.user.id,
        ServiceRequest.idempotency_key == idempotency_key,
    )
    existing = await db.scalar(retry_query)
    if existing is not None:
        if existing.payload_hash != digest:
            raise CRMConflict
        return _view(existing, house.area_id), False

    initial_status_id = await db.scalar(
        select(RequestStatus.id).where(
            RequestStatus.organization_id == payload.organization_id,
            RequestStatus.is_initial.is_(True),
        )
    )
    if initial_status_id is None:
        raise CRMConflict

    created = await db.scalar(
        insert(ServiceRequest)
        .values(
            **payload.model_dump(),
            status_id=initial_status_id,
            source="manual",
            created_by=authenticated.user.id,
            idempotency_key=idempotency_key,
            payload_hash=digest,
        )
        .on_conflict_do_nothing(
            index_elements=[
                ServiceRequest.organization_id,
                ServiceRequest.created_by,
                ServiceRequest.idempotency_key,
            ]
        )
        .returning(ServiceRequest)
    )
    if created is None:
        # ON CONFLICT waits for the winning transaction; READ COMMITTED then
        # sees its committed row. No duplicate history is added by the loser.
        existing = await db.scalar(retry_query)
        if existing is None or existing.payload_hash != digest:
            raise CRMConflict
        return _view(existing, house.area_id), False

    db.add(
        RequestStatusHistory(
            request_id=created.id,
            from_status_id=None,
            to_status_id=initial_status_id,
            actor_id=authenticated.user.id,
        )
    )
    db.add(
        AuditLog(
            action="request.created",
            actor_id=authenticated.user.id,
            target_type="requests",
            target_id=created.id,
        )
    )
    if commit:
        await db.commit()
    else:
        await db.flush()
    return _view(created, house.area_id), True


@router.post("", response_model=RequestRead, status_code=status.HTTP_201_CREATED)
async def create_request(
    payload: RequestCreate,
    response: Response,
    db: DatabaseDependency,
    authenticated: SessionDependency,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> RequestRead:
    result, created = await create_manual_request(db, authenticated, payload, idempotency_key)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    response.headers["Location"] = f"/api/requests/{result.id}"
    return result


@router.get("", response_model=RequestPage)
async def list_requests(
    filters: Annotated[RequestFilters, Query()],
    db: DatabaseDependency,
    authenticated: SessionDependency,
) -> RequestPage:
    access = await load_access(db, authenticated, filters.organization_id, "requests.view")
    query = _visible_requests(access)
    for field_name in ("house_id", "category_id", "status_id", "priority"):
        value = getattr(filters, field_name)
        if value is not None:
            query = query.where(getattr(ServiceRequest, field_name) == value)
    if filters.created_from is not None:
        query = query.where(ServiceRequest.created_at >= filters.created_from)
    if filters.created_to is not None:
        query = query.where(ServiceRequest.created_at <= filters.created_to)
    if filters.q:
        conditions = [
            field.icontains(filters.q, autoescape=True)
            for field in (
                ServiceRequest.description,
                ServiceRequest.applicant_name,
                ServiceRequest.applicant_phone,
                ServiceRequest.apartment,
            )
        ]
        if match := re.fullmatch(r"(?:REQ-)?([0-9]+)", filters.q, re.IGNORECASE):
            number = int(match.group(1))
            if number <= 2**63 - 1:
                conditions.append(ServiceRequest.number == number)
        query = query.where(or_(*conditions))
    query = (
        query.order_by(ServiceRequest.created_at.desc(), ServiceRequest.number.desc())
        .limit(filters.limit)
        .offset(filters.offset)
    )
    rows = await db.execute(query)
    return RequestPage(
        items=[_view(request, area_id) for request, area_id in rows],
        limit=filters.limit,
        offset=filters.offset,
    )


@router.get("/{request_id}", response_model=RequestRead)
async def request_detail(
    request_id: UUID, db: DatabaseDependency, authenticated: SessionDependency
) -> RequestRead:
    request, area_id = await _visible_request(db, authenticated, request_id)
    return _view(request, area_id)


@router.get("/{request_id}/history", response_model=RequestHistoryPage)
async def request_history(
    request_id: UUID, db: DatabaseDependency, authenticated: SessionDependency
) -> RequestHistoryPage:
    await _visible_request(db, authenticated, request_id)
    rows = await db.scalars(
        select(RequestStatusHistory)
        .where(RequestStatusHistory.request_id == request_id)
        .order_by(RequestStatusHistory.created_at, RequestStatusHistory.id)
    )
    return RequestHistoryPage(items=[RequestHistoryRead.model_validate(row) for row in rows])
