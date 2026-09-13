"""Short native dialogs; all state and replies commit with the incoming event."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.actor import Actor
from app.auth.service import InvalidCredentials
from app.bot.identity import access_stamp, native_actor
from app.bot.updates import IncomingUpdate
from app.core.config import Settings
from app.crm.access import access_from_membership, load_access
from app.crm.errors import CRMConflict, CRMNotFound, CRMPermissionDenied
from app.crm.request_schemas import RequestCreate
from app.crm.requests import create_manual_request
from app.crm.task_workflow import (
    PROGRESS_STATES,
    assign_request,
    list_tasks,
    report_progress,
    visible_task,
)
from app.models.bot import BotConversation, BotDelivery, ChatObservation, HouseChat
from app.models.crm import Category, Employee, House, RequestStatus, Role, ServiceRequest
from app.models.identity import User
from app.models.task_progress import TaskProgress

PAGE_SIZE = 8
Buttons = list[list[dict[str, str]]]


def button(text: str, payload: str) -> dict[str, str]:
    return {"text": text[:128], "payload": payload}


def menu(is_owner: bool = False) -> Buttons:
    return [
        [button("Мои задания", "list:mine:0"), button("Создать заявку", "new")],
        [button("Обращения из чатов", "inbox:0")],
        *([[button("Все заявки", "list:all:0")]] if is_owner else []),
    ]


async def reply(
    db: AsyncSession,
    actor: Actor,
    text: str,
    buttons: Buttons | None = None,
    *,
    callback_id: str | None = None,
) -> None:
    db.add(
        BotDelivery(
            max_user_id=actor.user.max_user_id,
            access_stamp=await access_stamp(db, actor),
            text=text[:4000],
            buttons=buttons,
            callback_id=callback_id,
        )
    )


async def conversation(db: AsyncSession, update: IncomingUpdate) -> BotConversation:
    now = datetime.now(UTC)
    await db.execute(
        insert(BotConversation)
        .values(
            max_user_id=update.actor_id,
            expires_at=now + timedelta(minutes=30),
        )
        .on_conflict_do_nothing(index_elements=[BotConversation.max_user_id])
    )
    result = await db.scalar(
        select(BotConversation)
        .where(
            BotConversation.max_user_id == update.actor_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert result is not None
    if result.expires_at < now:
        result.state, result.data = "menu", {}
    result.expires_at = now + timedelta(minutes=30)
    return result


async def task_card(
    db: AsyncSession,
    actor: Actor,
    task: ServiceRequest,
    prefix: str = "",
) -> None:
    house = await db.get(House, task.house_id)
    status = await db.get(RequestStatus, task.status_id)
    assignee = await db.get(Employee, task.assignee_id) if task.assignee_id else None
    reports = list(
        await db.scalars(
            select(TaskProgress)
            .where(
                TaskProgress.request_id == task.id,
            )
            .order_by(TaskProgress.created_at.desc(), TaskProgress.id.desc())
            .limit(3)
        )
    )
    if task.source == "resident_bot":
        creator = await db.get(User, task.created_by)
        lines = [
            prefix,
            f"Заявка №{task.number}",
            f"Имя: {task.applicant_name or '—'}",
            f"Адрес: {task.applicant_address or '—'}",
            f"Телефон: {task.applicant_phone or '—'}",
            f"MAX ID: {creator.max_user_id if creator else '—'}",
            f"Проблема: {task.description[:1400]}",
            f"Статус: {status.name if status else 'Новая'}",
            f"Исполнитель: {assignee.display_name if assignee else 'не назначен'}",
        ]
    else:
        lines = [
            prefix,
            f"Заявка №{task.number}",
            house.address if house else "",
            task.description[:1400],
            f"Статус: {status.name if status else 'Новая'}",
            f"Исполнитель: {assignee.display_name if assignee else 'не назначен'}",
        ]
    for report in reversed(reports):
        lines.append(
            PROGRESS_STATES.get(report.state, report.state)
            + (f": {report.note[:500]}" if report.note else "")
        )
    access = await load_access(db, actor, task.organization_id, "requests.view")
    rows: Buttons = []
    if "requests.update" in access.permissions and (
        actor.is_owner or (assignee and assignee.max_user_id == actor.user.max_user_id)
    ):
        rows.extend(
            [
                [
                    button("В работе", f"progress:{task.id}:{task.revision}:in_progress"),
                    button("Готово", f"progress:{task.id}:{task.revision}:done"),
                ],
                [button("Не выполнено", f"progress:{task.id}:{task.revision}:not_done")],
                [button("Для выполнения нужно…", f"progress:{task.id}:{task.revision}:needs")],
            ]
        )
    if "requests.assign" in access.permissions:
        rows.append([button("Назначить сотрудника", f"assignees:{task.id}:{task.revision}:0")])
    rows.extend([[button("Обновить", f"task:{task.id}"), button("Меню", "menu")]])
    await reply(db, actor, "\n".join(line for line in lines if line), rows)


async def notify_owners(
    db: AsyncSession,
    settings: Settings,
    task: ServiceRequest,
    *,
    exclude_id: int | None = None,
    prefix: str = "Сотрудник обновил заявку.",
) -> None:
    for max_id in settings.max_owner_ids:
        if max_id == exclude_id:
            continue
        try:
            owner = await native_actor(db, settings, max_id)
        except InvalidCredentials:
            continue
        await task_card(db, owner, task, prefix)


async def select_house(
    db: AsyncSession,
    actor: Actor,
    state: BotConversation,
    org_id: UUID,
    offset: int = 0,
) -> None:
    access = await load_access(db, actor, org_id, "requests.create")
    houses = list(
        await db.scalars(
            select(House)
            .where(access.house_predicate())
            .order_by(House.address, House.id)
            .limit(PAGE_SIZE + 1)
            .offset(offset)
        )
    )
    if not houses:
        await reply(
            db, actor, "Нет доступных домов. Обратитесь к руководителю.", menu(actor.is_owner)
        )
        return
    if len(houses) == 1 and offset == 0:
        state.data = state.data | {"house_id": str(houses[0].id)}
        await select_category(db, actor, state, org_id)
        return
    flow = state.data["flow"]
    rows = [[button(h.address, f"house:{flow}:{h.id}")] for h in houses[:PAGE_SIZE]]
    if len(houses) > PAGE_SIZE:
        rows.append([button("Дальше", f"houses:{flow}:{offset + PAGE_SIZE}")])
    rows.append([button("Отмена", "cancel")])
    await reply(db, actor, "Выберите дом.", rows)


async def select_category(
    db: AsyncSession,
    actor: Actor,
    state: BotConversation,
    org_id: UUID,
    offset: int = 0,
) -> None:
    access = await load_access(db, actor, org_id, "requests.create")
    query = select(Category).where(Category.organization_id == org_id)
    if not access.is_owner and not access.all_categories:
        query = query.where(Category.id.in_(access.category_ids))
    categories = list(
        await db.scalars(
            query.order_by(Category.name, Category.id).limit(PAGE_SIZE + 1).offset(offset)
        )
    )
    if not categories:
        await reply(
            db, actor, "Нет доступных категорий. Обратитесь к руководителю.", menu(actor.is_owner)
        )
        return
    state.state = "category"
    if len(categories) == 1 and offset == 0:
        state.state = "description"
        state.data = state.data | {"category_id": str(categories[0].id)}
        await reply(
            db,
            actor,
            "Что нужно сделать? Напишите одним сообщением.",
            [[button("Отмена", "cancel")]],
        )
        return
    flow = state.data["flow"]
    rows = [[button(c.name, f"category:{flow}:{c.id}")] for c in categories[:PAGE_SIZE]]
    if len(categories) > PAGE_SIZE:
        rows.append([button("Дальше", f"categories:{flow}:{offset + PAGE_SIZE}")])
    rows.append([button("Отмена", "cancel")])
    await reply(db, actor, "Выберите категорию.", rows)


async def show_inbox(db: AsyncSession, actor: Actor, org_id: UUID, offset: int) -> None:
    access = await load_access(db, actor, org_id, "requests.create")
    query = (
        select(ChatObservation, House.address)
        .select_from(ChatObservation)
        .join(HouseChat, ChatObservation.chat_id == HouseChat.chat_id)
        .join(House, House.id == HouseChat.house_id)
        .where(
            HouseChat.organization_id == org_id,
            access.house_predicate(),
            ChatObservation.request_id.is_(None),
            ChatObservation.dismissed.is_(False),
        )
    )
    if not access.is_owner and not access.all_categories:
        query = query.where(HouseChat.category_id.in_(access.category_ids))
    rows = (
        await db.execute(
            query.order_by(
                ChatObservation.important.desc(), ChatObservation.created_at, ChatObservation.id
            )
            .offset(offset)
            .limit(PAGE_SIZE + 1)
        )
    ).all()
    buttons = [
        [
            button(
                ("❗ " if item.important else "") + address[:45] + " · " + item.text[:50],
                f"observation:{item.id}",
            )
        ]
        for item, address in rows[:PAGE_SIZE]
    ]
    if len(rows) > PAGE_SIZE:
        buttons.append([button("Дальше", f"inbox:{offset + PAGE_SIZE}")])
    buttons.append([button("Меню", "menu")])
    await reply(
        db,
        actor,
        "Обращения из чатов. ❗ — возможная срочность по словам в сообщении."
        if rows
        else "Новых обращений из чатов нет.",
        buttons,
    )


async def observation_action(
    db: AsyncSession,
    actor: Actor,
    action: str,
    observation_id: UUID,
    event_key: str,
) -> None:
    item = await db.scalar(
        select(ChatObservation)
        .where(ChatObservation.id == observation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if item is None:
        raise CRMNotFound
    binding = await db.get(HouseChat, item.chat_id)
    assert binding is not None
    access = await load_access(db, actor, binding.organization_id, "requests.create")
    house = await db.get(House, binding.house_id)
    if house is None or not access.can_house(house) or not access.can_category(binding.category_id):
        raise CRMNotFound
    if item.request_id:
        await task_card(db, actor, await visible_task(db, actor, item.request_id))
    elif item.dismissed:
        await reply(db, actor, "Обращение уже разобрано.", menu(actor.is_owner))
    elif action == "convert":
        result, _ = await create_manual_request(
            db,
            actor,
            RequestCreate(
                organization_id=binding.organization_id,
                house_id=binding.house_id,
                category_id=binding.category_id,
                description=item.text,
                priority="high" if item.important else "normal",
            ),
            UUID(event_key[:32]),
            commit=False,
        )
        task = await visible_task(db, actor, result.id)
        task.source = "chat"
        item.request_id = task.id
        await task_card(db, actor, task, "Обращение сохранено как заявка.")
    elif action == "dismiss":
        item.dismissed = True
        await reply(db, actor, "Обращение разобрано без создания заявки.", menu(actor.is_owner))
    else:
        await reply(
            db,
            actor,
            f"{house.address}\n\n{item.text[:3400]}",
            [
                [
                    button("Создать заявку", f"convert:{item.id}"),
                    button("Не заявка", f"dismiss:{item.id}"),
                ],
                [button("К обращениям", "inbox:0")],
            ],
        )


def page(value: str) -> int:
    offset = int(value)
    if not 0 <= offset <= 10_000:
        raise ValueError("Invalid page")
    return offset


async def handle_staff(
    db: AsyncSession,
    settings: Settings,
    update: IncomingUpdate,
    actor: Actor,
    state: BotConversation,
) -> None:
    org_id = settings.max_bot_organization_id
    if org_id is None:
        await reply(db, actor, "Бот ещё не настроен. Руководитель завершит настройку при запуске.")
        return
    text = (update.text or "").strip() if update.update_type == "message_created" else ""
    payload = update.callback_payload or ""
    if update.update_type == "bot_started" or text.lower() in {"/start", "/help", "/menu"}:
        payload = "menu"
    elif text.lower() in {"/cancel", "отмена"}:
        payload = "cancel"
    elif text.lower() in {"/new", "новая заявка"}:
        payload = "new"
    elif text.lower() in {"/tasks", "мои задания"}:
        payload = "list:mine:0"
    if payload in {"menu", "cancel"}:
        state.state, state.data = "menu", {}
        await reply(
            db,
            actor,
            "Выберите действие. По заданию можно отметить результат или написать, что нужно.",
            menu(actor.is_owner),
        )
        return
    if payload == "new":
        state.state, state.data = "house", {"flow": uuid4().hex}
        await select_house(db, actor, state, org_id)
        return
    parts = payload.split(":")
    action = parts[0]
    if action in {
        "list",
        "task",
        "inbox",
        "observation",
        "convert",
        "dismiss",
        "assignees",
        "assign",
    }:
        state.state, state.data = "menu", {}
    if action in {"house", "houses", "category", "categories"} and len(parts) == 3:
        expected_state = "house" if action in {"house", "houses"} else "category"
        if state.data.get("flow") != parts[1] or state.state != expected_state:
            raise CRMConflict
        if action == "houses":
            await select_house(db, actor, state, org_id, page(parts[2]))
        elif action == "house":
            house = await db.get(House, UUID(parts[2]))
            access = await load_access(db, actor, org_id, "requests.create")
            if house is None or not access.can_house(house):
                raise CRMNotFound
            state.data = state.data | {"house_id": str(house.id)}
            await select_category(db, actor, state, org_id)
        elif action == "categories":
            await select_category(db, actor, state, org_id, page(parts[2]))
        else:
            category = await db.get(Category, UUID(parts[2]))
            access = await load_access(db, actor, org_id, "requests.create")
            if (
                category is None
                or category.organization_id != org_id
                or not access.can_category(category.id)
            ):
                raise CRMNotFound
            state.state = "description"
            state.data = state.data | {"category_id": str(category.id)}
            await reply(
                db,
                actor,
                "Что нужно сделать? Напишите одним сообщением.",
                [[button("Отмена", "cancel")]],
            )
        return
    if action == "list" and len(parts) == 3 and parts[1] in {"mine", "all"}:
        if parts[1] == "all":
            await load_access(db, actor, org_id, "requests.assign")
        offset = page(parts[2])
        tasks = await list_tasks(
            db, actor, org_id, only_mine=parts[1] == "mine", limit=PAGE_SIZE + 1, offset=offset
        )
        rows = [
            [button(f"№{task.number} · {task.description[:80]}", f"task:{task.id}")]
            for task in tasks[:PAGE_SIZE]
        ]
        if len(tasks) > PAGE_SIZE:
            rows.append([button("Дальше", f"list:{parts[1]}:{offset + PAGE_SIZE}")])
        rows.append([button("Меню", "menu")])
        await reply(db, actor, "Выберите заявку." if tasks else "Заданий пока нет.", rows)
        return
    if action == "task" and len(parts) == 2:
        await task_card(db, actor, await visible_task(db, actor, UUID(parts[1])))
        return
    if action in {"observation", "convert", "dismiss"} and len(parts) == 2:
        await observation_action(db, actor, action, UUID(parts[1]), update.event_key)
        return
    if action == "inbox" and len(parts) == 2:
        await show_inbox(db, actor, org_id, page(parts[1]))
        return
    if action == "assignees" and len(parts) == 4:
        task = await visible_task(db, actor, UUID(parts[1]))
        await load_access(db, actor, task.organization_id, "requests.assign")
        if task.revision != int(parts[2]):
            raise CRMConflict
        house = await db.get(House, task.house_id)
        assert house is not None
        allowed = (*settings.max_owner_ids, *settings.max_employee_ids)
        rows = []
        candidates = (
            await db.execute(
                select(Employee, Role)
                .join(Role, Employee.role_id == Role.id)
                .where(
                    Employee.organization_id == task.organization_id,
                    Employee.is_active.is_(True),
                    Employee.max_user_id.in_(allowed),
                )
                .order_by(Employee.display_name, Employee.id)
            )
        ).all()
        eligible: list[Employee] = []
        for employee, role in candidates:
            try:
                access = access_from_membership(employee, role)
            except CRMPermissionDenied:
                continue
            if (
                {"requests.view", "requests.update"} <= access.permissions
                and access.can_house(house)
                and access.can_category(task.category_id)
            ):
                eligible.append(employee)
        offset = page(parts[3])
        for employee in eligible[offset : offset + PAGE_SIZE]:
            rows.append(
                [button(employee.display_name, f"assign:{task.id}:{task.revision}:{employee.id}")]
            )
        if len(eligible) > offset + PAGE_SIZE:
            rows.append(
                [button("Дальше", f"assignees:{task.id}:{task.revision}:{offset + PAGE_SIZE}")]
            )
        rows.append([button("К заявке", f"task:{task.id}")])
        await reply(
            db, actor, "Выберите исполнителя." if eligible else "Нет доступных исполнителей.", rows
        )
        return
    if action == "assign" and len(parts) == 4:
        task = await assign_request(
            db,
            actor,
            UUID(parts[1]),
            UUID(parts[3]),
            int(parts[2]),
            (*settings.max_owner_ids, *settings.max_employee_ids),
        )
        employee = await db.get(Employee, task.assignee_id)
        assert employee is not None
        recipient = await native_actor(db, settings, employee.max_user_id, employee.display_name)
        await task_card(db, recipient, task, "Вам назначена заявка.")
        await task_card(db, actor, task, "Исполнитель назначен.")
        return
    if action == "progress" and len(parts) == 4 and parts[3] in PROGRESS_STATES:
        task = await visible_task(db, actor, UUID(parts[1]))
        if parts[3] in {"needs", "not_done"}:
            # Mutation rechecks assignee, scopes and revision after the text arrives.
            if task.revision != int(parts[2]):
                raise CRMConflict
            state.state, state.data = (
                "report",
                {"request_id": str(task.id), "revision": parts[2], "status": parts[3]},
            )
            await reply(
                db,
                actor,
                "Что нужно для выполнения? Например: нужна лестница и напарник."
                if parts[3] == "needs"
                else "Почему не выполнено? Напишите причину.",
                [[button("Отмена", "cancel")]],
            )
        else:
            state.state, state.data = "menu", {}
            task = await report_progress(db, actor, task.id, parts[3], None, int(parts[2]))
            await task_card(db, actor, task, "Результат сохранён.")
            await notify_owners(db, settings, task, exclude_id=actor.user.max_user_id)
        return
    if text and not text.startswith("/") and state.state == "report":
        if len(text) > 2000:
            await reply(
                db, actor, "Пояснение должно быть не длиннее 2000 символов. Сократите сообщение."
            )
            return
        task = await report_progress(
            db,
            actor,
            UUID(state.data["request_id"]),
            state.data["status"],
            text,
            int(state.data["revision"]),
        )
        state.state, state.data = "menu", {}
        await task_card(db, actor, task, "Пояснение сохранено.")
        await notify_owners(db, settings, task, exclude_id=actor.user.max_user_id)
        return
    if text and not text.startswith("/") and state.state == "description":
        result, _ = await create_manual_request(
            db,
            actor,
            RequestCreate(
                organization_id=org_id,
                house_id=UUID(state.data["house_id"]),
                category_id=UUID(state.data["category_id"]),
                description=text,
            ),
            UUID(update.event_key[:32]),
            commit=False,
        )
        state.state, state.data = "menu", {}
        task = await visible_task(db, actor, result.id)
        task.source = "staff_bot"
        await task_card(db, actor, task, "Заявка создана.")
        await notify_owners(
            db, settings, task, exclude_id=actor.user.max_user_id, prefix="Новая заявка."
        )
        return
    await reply(
        db,
        actor,
        "Выберите действие кнопкой. Для нового обращения нажмите «Создать заявку».",
        menu(actor.is_owner),
    )
