"""Simple public resident intake and read-only access to own requests."""

import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.actor import Actor
from app.bot.dialogs import notify_owners
from app.bot.ui import Buttons, button, reply
from app.bot.updates import IncomingUpdate
from app.core.config import Settings
from app.crm.errors import CRMError
from app.crm.task_workflow import resident_problem_remains
from app.models.bot import BotConversation
from app.models.crm import Category, House, RequestStatus, RequestStatusHistory, ServiceRequest
from app.models.identity import AuditLog

PAGE_SIZE = 8
RESIDENT_STATUS = {
    "new": "Принята",
    "in_progress": "В работе",
    "done": "На проверке",
    "closed": "Выполнено",
    "resident_issue": "Передано оператору",
    "not_done": "В работе",
    "needs": "В работе",
}


def resident_menu() -> Buttons:
    return [
        [button("Создать заявку", "resident_new")],
        [button("Мои заявки", "resident_mine:0")],
    ]


def _clean(value: str, *, max_length: int, min_length: int = 1) -> str | None:
    result = value.strip()
    if not min_length <= len(result) <= max_length:
        return None
    return result


async def _create_request(
    db: AsyncSession,
    settings: Settings,
    actor: Actor,
    data: dict[str, str],
) -> ServiceRequest:
    organization_id = settings.max_bot_organization_id
    if organization_id is None:
        raise ValueError("Bot organization is not configured")
    house = await db.scalar(
        select(House).where(House.organization_id == organization_id).order_by(House.id).limit(1)
    )
    category = await db.scalar(
        select(Category)
        .where(Category.organization_id == organization_id)
        .order_by(Category.id)
        .limit(1)
    )
    status = await db.scalar(
        select(RequestStatus).where(
            RequestStatus.organization_id == organization_id,
            RequestStatus.is_initial.is_(True),
        )
    )
    if house is None or category is None or status is None:
        raise ValueError("Bot catalog is incomplete")

    idempotency_key = UUID(data["flow"])
    existing = await db.scalar(
        select(ServiceRequest).where(
            ServiceRequest.organization_id == organization_id,
            ServiceRequest.created_by == actor.user.id,
            ServiceRequest.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return existing

    payload = {
        "name": data["name"],
        "address": data["address"],
        "problem": data["problem"],
        "phone": data["phone"],
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    task = ServiceRequest(
        organization_id=organization_id,
        house_id=house.id,
        category_id=category.id,
        status_id=status.id,
        applicant_name=data["name"],
        applicant_address=data["address"],
        applicant_phone=data["phone"],
        description=data["problem"],
        priority="normal",
        source="resident_bot",
        created_by=actor.user.id,
        idempotency_key=idempotency_key,
        payload_hash=digest,
    )
    db.add(task)
    await db.flush()
    db.add_all(
        [
            RequestStatusHistory(
                request_id=task.id,
                from_status_id=None,
                to_status_id=status.id,
                actor_id=actor.user.id,
            ),
            AuditLog(
                action="request.created",
                actor_id=actor.user.id,
                target_type="requests",
                target_id=task.id,
            ),
        ]
    )
    return task


async def _show_my_requests(
    db: AsyncSession,
    actor: Actor,
    settings: Settings,
    offset: int,
) -> None:
    organization_id = settings.max_bot_organization_id
    if organization_id is None:
        await reply(db, actor, "Бот ещё не настроен.")
        return
    if not 0 <= offset <= 10_000:
        offset = 0
    rows = (
        await db.execute(
            select(ServiceRequest, RequestStatus)
            .join(RequestStatus, RequestStatus.id == ServiceRequest.status_id)
            .where(
                ServiceRequest.organization_id == organization_id,
                ServiceRequest.created_by == actor.user.id,
                ServiceRequest.source == "resident_bot",
            )
            .order_by(ServiceRequest.created_at.desc(), ServiceRequest.number.desc())
            .offset(offset)
            .limit(PAGE_SIZE + 1)
        )
    ).all()
    buttons: Buttons = []
    for task, status in rows[:PAGE_SIZE]:
        visible_status = RESIDENT_STATUS.get(status.code, status.name)
        buttons.append([button(f"№{task.number} · {visible_status}", f"resident_task:{task.id}")])
    if len(rows) > PAGE_SIZE:
        buttons.append([button("Дальше", f"resident_mine:{offset + PAGE_SIZE}")])
    buttons.append([button("Создать заявку", "resident_new")])
    buttons.append([button("Меню", "resident_menu")])
    await reply(
        db,
        actor,
        "Ваши заявки:" if rows else "У вас пока нет заявок.",
        buttons,
    )


async def _show_request(
    db: AsyncSession,
    actor: Actor,
    settings: Settings,
    request_id: UUID,
) -> None:
    organization_id = settings.max_bot_organization_id
    if organization_id is None:
        await reply(db, actor, "Бот ещё не настроен.")
        return
    row = (
        await db.execute(
            select(ServiceRequest, RequestStatus)
            .join(RequestStatus, RequestStatus.id == ServiceRequest.status_id)
            .where(
                ServiceRequest.id == request_id,
                ServiceRequest.organization_id == organization_id,
                ServiceRequest.created_by == actor.user.id,
                ServiceRequest.source == "resident_bot",
            )
        )
    ).one_or_none()
    if row is None:
        await reply(
            db,
            actor,
            "Заявка не найдена.",
            [[button("Мои заявки", "resident_mine:0"), button("Меню", "resident_menu")]],
        )
        return
    task, status = row
    visible_status = RESIDENT_STATUS.get(status.code, status.name)
    buttons: Buttons = []
    if status.code == "closed":
        buttons.append([button("Проблема осталась", f"resident_issue:{task.id}:{task.revision}")])
    buttons.extend(
        [
            [button("Мои заявки", "resident_mine:0")],
            [button("Создать заявку", "resident_new"), button("Меню", "resident_menu")],
        ]
    )
    await reply(
        db,
        actor,
        "\n".join(
            [
                f"Заявка №{task.number}",
                f"Статус: {visible_status}",
                f"Имя: {task.applicant_name or '—'}",
                f"Адрес: {task.applicant_address or '—'}",
                f"Что произошло: {task.description}",
                f"Телефон: {task.applicant_phone or '—'}",
            ]
        )[:4000],
        buttons,
    )


async def _start_intake(db: AsyncSession, actor: Actor, state: BotConversation) -> None:
    state.state = "resident_name"
    state.data = {"flow": uuid4().hex}
    await reply(
        db,
        actor,
        "1. Как вас зовут?\n\nНапишите имя одним сообщением.",
        [[button("Отменить", f"resident_cancel:{state.data['flow']}")]],
    )


async def handle_resident(
    db: AsyncSession,
    settings: Settings,
    update: IncomingUpdate,
    actor: Actor,
    state: BotConversation,
) -> None:
    text = (update.text or "").strip() if update.update_type == "message_created" else ""
    payload = update.callback_payload or ""
    command = text.casefold()

    if update.update_type == "bot_started" or command in {"/start", "/help", "/menu"}:
        payload = "resident_menu"
    elif command in {"/new", "создать заявку"}:
        payload = "resident_new"
    elif command in {"/tasks", "мои заявки"}:
        payload = "resident_mine:0"
    elif command in {"/cancel", "отмена"}:
        payload = "resident_cancel"

    if payload == "resident_menu":
        state.state, state.data = "menu", {}
        await reply(db, actor, "Что хотите сделать?", resident_menu())
        return
    if payload == "resident_new":
        await _start_intake(db, actor, state)
        return
    if payload.startswith("resident_mine:"):
        state.state, state.data = "menu", {}
        try:
            offset = int(payload.rsplit(":", 1)[1])
        except ValueError:
            offset = 0
        await _show_my_requests(db, actor, settings, offset)
        return
    if payload.startswith("resident_task:"):
        state.state, state.data = "menu", {}
        try:
            request_id = UUID(payload.rsplit(":", 1)[1])
        except ValueError:
            await reply(db, actor, "Заявка не найдена.", resident_menu())
            return
        await _show_request(db, actor, settings, request_id)
        return
    if payload.startswith("resident_issue:"):
        parts = payload.split(":")
        if len(parts) != 3:
            await reply(db, actor, "Эта кнопка уже неактуальна.", resident_menu())
            return
        try:
            request_id = UUID(parts[1])
            revision = int(parts[2])
            task = await resident_problem_remains(db, actor, request_id, revision)
        except (ValueError, CRMError):
            await reply(
                db,
                actor,
                "Статус заявки уже изменился. Откройте её заново.",
                resident_menu(),
            )
            return
        state.state, state.data = "menu", {}
        await reply(
            db,
            actor,
            f"Сообщили оператору по заявке №{task.number}, что проблема осталась.",
            [
                [button("Открыть заявку", f"resident_task:{task.id}")],
                [button("Меню", "resident_menu")],
            ],
        )
        await notify_owners(
            db,
            settings,
            task,
            prefix="Житель сообщил: проблема осталась.",
        )
        return
    if payload.startswith("resident_cancel"):
        flow = payload.split(":", 1)[1] if ":" in payload else None
        if flow is None or flow == state.data.get("flow"):
            state.state, state.data = "menu", {}
        await reply(db, actor, "Заявка отменена.", resident_menu())
        return
    if payload.startswith("resident_confirm:"):
        flow = payload.split(":", 1)[1]
        if state.state != "resident_confirm" or state.data.get("flow") != flow:
            await reply(db, actor, "Эта форма уже неактуальна.", resident_menu())
            return
        task = await _create_request(db, settings, actor, state.data)
        state.state, state.data = "menu", {}
        await reply(
            db,
            actor,
            f"Заявка №{task.number} создана и передана оператору.",
            resident_menu(),
        )
        await notify_owners(
            db,
            settings,
            task,
            prefix="Новая заявка от жителя.",
        )
        return

    if update.update_type != "message_created":
        await reply(db, actor, "Что хотите сделать?", resident_menu())
        return

    if state.state == "resident_name":
        value = _clean(text, max_length=200)
        if value is None:
            await reply(db, actor, "Напишите имя одним сообщением, не длиннее 200 символов.")
            return
        state.data = state.data | {"name": value}
        state.state = "resident_address"
        await reply(db, actor, "2. Напишите полный адрес.")
        return

    if state.state == "resident_address":
        value = _clean(text, max_length=500, min_length=3)
        if value is None:
            await reply(db, actor, "Напишите полный адрес одним сообщением.")
            return
        state.data = state.data | {"address": value}
        state.state = "resident_problem"
        await reply(db, actor, "3. Что произошло?")
        return

    if state.state == "resident_problem":
        value = _clean(text, max_length=10_000)
        if value is None:
            await reply(db, actor, "Опишите, что произошло, одним сообщением.")
            return
        state.data = state.data | {"problem": value}
        state.state = "resident_phone"
        await reply(db, actor, "4. Телефон для связи.")
        return

    if state.state == "resident_phone":
        value = _clean(text, max_length=50, min_length=3)
        if value is None:
            await reply(db, actor, "Напишите телефон для связи одним сообщением.")
            return
        state.data = state.data | {"phone": value}
        state.state = "resident_confirm"
        flow = state.data["flow"]
        await reply(
            db,
            actor,
            "\n".join(
                [
                    "Проверьте данные.",
                    "",
                    f"Имя: {state.data['name']}",
                    f"Адрес: {state.data['address']}",
                    f"Что произошло: {state.data['problem']}",
                    f"Телефон: {state.data['phone']}",
                ]
            )[:4000],
            [
                [
                    button("Подтвердить", f"resident_confirm:{flow}"),
                    button("Отменить", f"resident_cancel:{flow}"),
                ]
            ],
        )
        return

    await reply(db, actor, "Что хотите сделать?", resident_menu())
