"""Native MAX flows against PostgreSQL, with no live bot credentials or sends."""

import asyncio
import itertools
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from asgi_lifespan import LifespanManager
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema

from app.api.dependencies import get_db
from app.bot.ui import reply
from app.bot.identity import native_actor, resident_actor
from app.bot.setup import bind_chat, initialize
from app.bot.worker import deliver_one
from app.core.config import Settings
from app.integrations.max.messaging import MaxMessagingClient
from app.main import create_app
from app.models.base import Base
from app.models.bot import (
    BotConversation,
    BotDelivery,
    BotGroupChat,
    BotOrganizationSettings,
    BotReceipt,
    ChatObservation,
)
from app.models.crm import (
    Category,
    Employee,
    House,
    Organization,
    RequestStatus,
    Role,
    ServiceRequest,
)
from app.models.task_progress import TaskProgress

pytestmark = pytest.mark.integration
STAFF_SECRET = "synthetic_staff_secret_000000000000"
OBSERVER_SECRET = "synthetic_observer_secret_000000000"
EVENT_TIME = itertools.count(1_800_000_000_000)


@pytest.fixture
async def bot_catalog(db_session: AsyncSession, test_settings: Settings) -> Organization:
    test_settings.max_staff_webhook_secret = SecretStr(STAFF_SECRET)
    test_settings.max_observer_webhook_secret = SecretStr(OBSERVER_SECRET)
    test_settings.max_observer_token = SecretStr("synthetic-observer-token")
    org = await initialize(db_session, test_settings, "Тестовая УК", ["Тестовый дом 1"])
    test_settings.max_bot_organization_id = org.id
    await db_session.commit()
    return org


def event(
    actor_id: int = 101,
    *,
    text: str | None = None,
    payload: str | None = None,
    chat_type: str = "dialog",
    chat_id: int = 123,
) -> dict[str, object]:
    timestamp = next(EVENT_TIME)
    actor = {"user_id": actor_id, "first_name": f"Сотрудник {actor_id}", "is_bot": False}
    message = {
        "sender": actor,
        "recipient": {"chat_type": chat_type, "chat_id": chat_id},
        "body": {"mid": str(uuid4()), "text": text},
        "timestamp": timestamp,
    }
    if payload is not None:
        message["sender"] = {"user_id": 777, "first_name": "Bot", "is_bot": True}
        return {
            "update_type": "message_callback",
            "timestamp": timestamp,
            "message": message,
            "callback": {
                "callback_id": str(uuid4()),
                "timestamp": timestamp,
                "user": actor,
                "payload": payload,
            },
        }
    return {"update_type": "message_created", "timestamp": timestamp, "message": message}


async def send(client: AsyncClient, data: dict[str, object], namespace: str = "staff") -> None:
    result = await client.post(
        f"/api/bots/max/{namespace}",
        json=data,
        headers={
            "X-Max-Bot-Api-Secret": STAFF_SECRET if namespace == "staff" else OBSERVER_SECRET,
        },
    )
    assert result.status_code == 200, result.text


async def latest_text(db: AsyncSession, max_id: int = 101) -> str:
    result = await db.scalar(
        select(BotDelivery.text)
        .where(BotDelivery.max_user_id == max_id, BotDelivery.callback_id.is_(None))
        .order_by(BotDelivery.sequence.desc())
        .limit(1)
    )
    assert result is not None
    return result


async def create_task(client: AsyncClient, db: AsyncSession) -> ServiceRequest:
    await send(client, event(text="/new"))
    assert "Что нужно сделать" in await latest_text(db)
    await send(client, event(text="Починить свет на лестнице"))
    task = await db.scalar(select(ServiceRequest).order_by(ServiceRequest.number.desc()).limit(1))
    assert task is not None
    return task


async def test_employee_work_cycle_and_legacy_api(
    client: AsyncClient,
    db_session: AsyncSession,
    bot_catalog: Organization,
    owner_headers: dict[str, str],
) -> None:
    await send(client, event(text="/start"))
    await send(client, event(202, text="/start"))
    task = await create_task(client, db_session)
    employee = await db_session.scalar(select(Employee).where(Employee.max_user_id == 202))
    assert employee is not None
    assert employee.display_name == "Сотрудник 202"
    await send(client, event(payload=f"assign:{task.id}:0:{employee.id}"))
    await db_session.refresh(task)
    assert task.assignee_id == employee.id and task.revision == 1
    assert "Вам назначено задание" in await latest_text(db_session, 202)
    await send(client, event(202, payload=f"progress:{task.id}:1:needs"))
    assert "Что нужно для выполнения" in await latest_text(db_session, 202)
    report = event(202, text="Нужна лестница и напарник")
    await send(client, report)
    await send(client, report)
    await db_session.refresh(task)
    assert task.revision == 2
    assert await db_session.scalar(select(func.count()).select_from(TaskProgress)) == 1
    assert "Нужна лестница и напарник" in await latest_text(db_session, 101)
    await send(client, event(202, payload=f"progress:{task.id}:2:in_progress"))
    await send(client, event(202, payload=f"progress:{task.id}:3:not_done"))
    await send(client, event(202, text="Не попали в подъезд"))
    await send(client, event(202, payload=f"progress:{task.id}:4:done"))
    await db_session.refresh(task)
    status = await db_session.get(RequestStatus, task.status_id)
    assert status is not None and status.code == "done" and task.revision == 5
    assert await db_session.scalar(select(func.count()).select_from(TaskProgress)) == 4
    response = await client.get(f"/api/requests/{task.id}", headers=owner_headers)
    assert response.status_code == 200 and response.json()["source"] == "staff_bot"
    response = await client.get(
        "/api/requests", params={"organization_id": str(bot_catalog.id)}, headers=owner_headers
    )
    assert response.status_code == 200 and len(response.json()["items"]) == 1


async def test_observer_intake_triage_and_duplicate_conversion(
    client: AsyncClient,
    db_session: AsyncSession,
    bot_catalog: Organization,
) -> None:
    house = await db_session.scalar(select(House))
    category = await db_session.scalar(select(Category))
    assert house and category
    await bind_chat(db_session, -901, house.id, category.id)
    await db_session.commit()
    update = event(999, text="Прорвало трубу, затопило подвал", chat_type="chat", chat_id=-901)
    await send(client, update, "observer")
    await send(client, update, "observer")
    item = await db_session.scalar(select(ChatObservation))
    assert item and item.important
    assert await db_session.scalar(select(func.count()).select_from(ChatObservation)) == 1
    assert await db_session.scalar(select(func.count()).select_from(ServiceRequest)) == 0
    assert await db_session.scalar(select(func.count()).select_from(BotDelivery)) == 0
    await send(client, event(payload="inbox:0"))
    assert "Обращения из чатов" in await latest_text(db_session)
    await send(client, event(payload=f"observation:{item.id}"))
    assert "Прорвало" in await latest_text(db_session)
    await send(client, event(payload=f"convert:{item.id}"))
    await send(client, event(payload=f"convert:{item.id}"))
    await db_session.refresh(item)
    task = await db_session.scalar(select(ServiceRequest))
    assert task and item.request_id == task.id and task.source == "chat"
    assert await db_session.scalar(select(func.count()).select_from(ServiceRequest)) == 1


async def test_unbound_chat_and_public_staff_actions_are_ignored(
    client: AsyncClient,
    db_session: AsyncSession,
    bot_catalog: Organization,
) -> None:
    await send(client, event(text="/new", chat_type="chat"))
    await send(client, event(payload="new", chat_type="chat"))
    await send(client, event(999, text="авария", chat_type="chat", chat_id=-999), "observer")
    assert await db_session.scalar(select(func.count()).select_from(BotDelivery)) == 0
    assert await db_session.scalar(select(func.count()).select_from(ChatObservation)) == 0


async def test_webhook_secret_before_body_and_size_limit(
    client: AsyncClient,
    db_session: AsyncSession,
    bot_catalog: Organization,
) -> None:
    response = await client.post("/api/bots/max/staff", content="not-json")
    assert response.status_code == 403
    response = await client.post(
        "/api/bots/max/staff",
        json=event(text="/new"),
        headers={"X-Max-Bot-Api-Secret": OBSERVER_SECRET},
    )
    assert response.status_code == 403
    response = await client.post(
        "/api/bots/max/staff",
        content=b"x" * (256 * 1024 + 1),
        headers={"X-Max-Bot-Api-Secret": STAFF_SECRET},
    )
    assert response.status_code == 413
    assert await db_session.scalar(select(func.count()).select_from(BotReceipt)) == 0


async def test_cancel_expiry_and_duplicate_text_do_not_create_requests(
    client: AsyncClient,
    db_session: AsyncSession,
    bot_catalog: Organization,
) -> None:
    await send(client, event(text="/new"))
    await send(client, event(text="/cancel"))
    await send(client, event(text="Не создавать"))
    assert await db_session.scalar(select(func.count()).select_from(ServiceRequest)) == 0
    await send(client, event(text="/new"))
    state = await db_session.get(BotConversation, 101)
    assert state
    state.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    await send(client, event(text="Просроченный черновик"))
    assert await db_session.scalar(select(func.count()).select_from(ServiceRequest)) == 0
    await send(client, event(text="/new"))
    data = event(text="Только одна заявка")
    await send(client, data)
    await send(client, data)
    assert await db_session.scalar(select(func.count()).select_from(ServiceRequest)) == 1


async def test_old_button_and_lost_assignment_cannot_change_task(
    client: AsyncClient,
    db_session: AsyncSession,
    bot_catalog: Organization,
) -> None:
    task = await create_task(client, db_session)
    employee = await db_session.scalar(select(Employee).where(Employee.max_user_id == 202))
    owner = await db_session.scalar(select(Employee).where(Employee.max_user_id == 101))
    assert employee and owner
    await send(client, event(payload=f"assign:{task.id}:0:{employee.id}"))
    await send(client, event(202, payload=f"progress:{task.id}:1:needs"))
    await send(client, event(payload=f"assign:{task.id}:1:{owner.id}"))
    await send(client, event(202, text="Чужое пояснение после переназначения"))
    assert await db_session.scalar(select(func.count()).select_from(TaskProgress)) == 0
    await send(client, event(payload=f"progress:{task.id}:1:done"))
    assert "уже изменились" in await latest_text(db_session)
    await db_session.refresh(task)
    assert task.revision == 2


async def test_scope_revocation_during_creation(
    client: AsyncClient,
    db_session: AsyncSession,
    bot_catalog: Organization,
) -> None:
    await send(client, event(202, text="/new"))
    employee = await db_session.scalar(select(Employee).where(Employee.max_user_id == 202))
    assert employee
    employee.all_houses = False
    await db_session.commit()
    await send(client, event(202, text="Заявка после отзыва доступа"))
    assert await db_session.scalar(select(func.count()).select_from(ServiceRequest)) == 0


@pytest.mark.parametrize("result", ["success", "timeout", "denied"])
async def test_outgoing_delivery_once(
    db_session: AsyncSession,
    test_settings: Settings,
    bot_catalog: Organization,
    result: str,
) -> None:
    actor = await native_actor(db_session, test_settings, 101, "Руководитель")
    await reply(db_session, actor, "Сохранённый ответ")
    await db_session.commit()
    calls: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if result == "timeout":
            raise httpx.ReadTimeout("synthetic private request", request=request)
        if result == "denied":
            return httpx.Response(403)
        return httpx.Response(200, json={"message": {"body": {"mid": "synthetic-mid"}}})

    async with MaxMessagingClient(
        token=SecretStr("synthetic-token"), transport=httpx.MockTransport(transport)
    ) as max_client:
        assert await deliver_one(db_session, test_settings, max_client)
        assert not await deliver_one(db_session, test_settings, max_client)
    assert len(calls) == 1
    row = await db_session.scalar(select(BotDelivery))
    assert (
        row and row.state == {"success": "sent", "timeout": "uncertain", "denied": "failed"}[result]
    )
    assert row.text == "" and row.buttons is None


@pytest.mark.parametrize("revocation", ["membership", "scope"])
async def test_queued_private_content_rechecks_current_access(
    db_session: AsyncSession,
    test_settings: Settings,
    bot_catalog: Organization,
    revocation: str,
) -> None:
    actor = await native_actor(db_session, test_settings, 202, "Исполнитель")
    await reply(db_session, actor, "Приватная карточка")
    employee = await db_session.scalar(select(Employee).where(Employee.max_user_id == 202))
    assert employee
    if revocation == "membership":
        employee.is_active = False
    else:
        employee.all_houses = False
    await db_session.commit()

    def transport(request: httpx.Request) -> httpx.Response:
        pytest.fail("Revoked recipient must not receive the queued content")

    async with MaxMessagingClient(
        token=SecretStr("synthetic-token"), transport=httpx.MockTransport(transport)
    ) as max_client:
        assert await deliver_one(db_session, test_settings, max_client)
    row = await db_session.scalar(select(BotDelivery))
    assert row and row.state == "discarded" and row.text == ""


async def test_setup_is_repeatable_and_preserves_employee_scopes(
    db_session: AsyncSession,
    test_settings: Settings,
    bot_catalog: Organization,
) -> None:
    employee = await db_session.scalar(select(Employee).where(Employee.max_user_id == 202))
    assert employee
    employee.all_houses = False
    original_role_id = employee.role_id
    test_settings.max_operator_ids = (*test_settings.max_operator_ids, 202)
    test_settings.max_employee_ids = ()
    await initialize(
        db_session, test_settings, "Не переименовывать", ["Тестовый дом 1"], bot_catalog.id
    )
    await db_session.refresh(employee)
    role = await db_session.get(Role, employee.role_id)
    assert employee.role_id == original_role_id
    assert role is not None and role.name == "Сотрудник бота"
    assert "requests.assign" not in role.permissions
    assert not employee.all_houses and bot_catalog.name == "Тестовая УК"
    assert await db_session.scalar(select(func.count()).select_from(House)) == 1
    with pytest.raises(ValueError, match="Catalog already exists"):
        await initialize(db_session, test_settings, "Другая УК", ["Другой дом"])
    with pytest.raises(ValueError, match="same organization"):
        await bind_chat(db_session, -22, uuid4(), uuid4())


async def test_simultaneous_webhook_retries_commit_one_request(test_settings: Settings) -> None:
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL must point to a disposable PostgreSQL database")
    schema = "bot_concurrency_" + uuid4().hex
    engine = create_async_engine(
        test_settings.database_url.get_secret_value(),
        hide_parameters=True,
        execution_options={"schema_translate_map": {None: schema}},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    test_settings.max_staff_webhook_secret = SecretStr(STAFF_SECRET)
    barrier = asyncio.Barrier(2)
    simultaneous = False
    try:
        async with engine.begin() as connection:
            await connection.execute(CreateSchema(schema))
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as setup:
            org = await initialize(setup, test_settings, "Race УК", ["Race дом"])
            test_settings.max_bot_organization_id = org.id
            await setup.commit()
        app = create_app(test_settings)

        async def connection_for_event() -> AsyncIterator[AsyncSession]:
            async with factory() as db:
                if simultaneous:
                    await asyncio.wait_for(barrier.wait(), timeout=10)
                yield db

        app.dependency_overrides[get_db] = connection_for_event
        async with (
            LifespanManager(app),
            AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        ):
            await send(client, event(text="/new"))
            simultaneous = True
            data = event(text="Одна заявка из двух параллельных доставок")
            await asyncio.wait_for(
                asyncio.gather(send(client, data), send(client, data)), timeout=20
            )
        async with factory() as verify:
            assert await verify.scalar(select(func.count()).select_from(ServiceRequest)) == 1
            assert await verify.scalar(select(func.count()).select_from(BotReceipt)) == 2
            assert await verify.scalar(select(func.count()).select_from(BotDelivery)) == 3
            assert (
                await verify.scalar(
                    select(func.count())
                    .select_from(BotDelivery)
                    .where(BotDelivery.max_user_id == 404, BotDelivery.callback_id.is_(None))
                )
                == 1
            )
    finally:
        # The isolated schema lives only in the disposable container; no DROP or shared cleanup.
        await engine.dispose()


async def test_interrupted_send_and_expired_queue_are_not_replayed(
    db_session: AsyncSession,
    test_settings: Settings,
    bot_catalog: Organization,
) -> None:
    actor = await native_actor(db_session, test_settings, 101, "Руководитель")
    await reply(db_session, actor, "Неопределённая доставка")
    await db_session.flush()
    row = await db_session.scalar(select(BotDelivery))
    assert row
    row.state = "sending"
    row.attempted_at = datetime.now(UTC) - timedelta(minutes=3)
    await reply(db_session, actor, "Слишком старый ответ")
    await db_session.flush()
    expired = await db_session.scalar(select(BotDelivery).where(BotDelivery.state == "pending"))
    assert expired
    expired.created_at = datetime.now(UTC) - timedelta(minutes=31)
    await db_session.commit()

    def transport(request: httpx.Request) -> httpx.Response:
        pytest.fail("Interrupted or expired delivery must not be sent")

    async with MaxMessagingClient(
        token=SecretStr("synthetic-token"), transport=httpx.MockTransport(transport)
    ) as max_client:
        assert await deliver_one(db_session, test_settings, max_client)
        assert not await deliver_one(db_session, test_settings, max_client)
    await db_session.refresh(row)
    await db_session.refresh(expired)
    assert row.state == "uncertain" and row.error_code == "interrupted"
    assert expired.state == "discarded" and expired.error_code == "expired"


async def test_callback_ack_accepts_long_provider_id(
    client: AsyncClient,
    db_session: AsyncSession,
    bot_catalog: Organization,
) -> None:
    data = event(payload="menu")
    assert isinstance(data["callback"], dict)
    data["callback"]["callback_id"] = "x" * 2048
    await send(client, data)
    ack = await db_session.scalar(select(BotDelivery).where(BotDelivery.callback_id.is_not(None)))
    assert ack and ack.callback_id == "x" * 2048


async def test_selecting_card_cancels_pending_note(
    client: AsyncClient,
    db_session: AsyncSession,
    bot_catalog: Organization,
) -> None:
    task = await create_task(client, db_session)
    await send(client, event(payload=f"progress:{task.id}:0:needs"))
    await send(client, event(payload=f"task:{task.id}"))
    await send(client, event(text="Этот текст уже не должен попасть в пояснение"))
    assert await db_session.scalar(select(func.count()).select_from(TaskProgress)) == 0


async def test_resident_intake_confirmation_and_own_requests(
    client: AsyncClient,
    db_session: AsyncSession,
    bot_catalog: Organization,
) -> None:
    resident_id = 303
    await send(client, event(resident_id, text="/start"))
    assert "Что хотите сделать" in await latest_text(db_session, resident_id)

    await send(client, event(resident_id, payload="resident_new"))
    assert "Как вас зовут" in await latest_text(db_session, resident_id)
    await send(client, event(resident_id, text="Анна Петровна"))
    assert "полный адрес" in await latest_text(db_session, resident_id)
    await send(client, event(resident_id, text="г. Тест, ул. Ленина, 12, кв. 45"))
    assert "Что произошло" in await latest_text(db_session, resident_id)
    await send(client, event(resident_id, text="Течёт труба под раковиной"))
    assert "Телефон" in await latest_text(db_session, resident_id)
    await send(client, event(resident_id, text="+7 999 123-45-67"))

    confirmation = await latest_text(db_session, resident_id)
    assert "Проверьте данные" in confirmation
    assert "Анна Петровна" in confirmation
    assert "ул. Ленина, 12" in confirmation
    assert await db_session.scalar(select(func.count()).select_from(ServiceRequest)) == 0

    state = await db_session.get(BotConversation, resident_id)
    assert state and state.state == "resident_confirm"
    flow = state.data["flow"]
    await send(client, event(resident_id, payload=f"resident_confirm:{flow}"))

    task = await db_session.scalar(
        select(ServiceRequest).where(ServiceRequest.source == "resident_bot")
    )
    assert task is not None
    assert task.applicant_name == "Анна Петровна"
    assert task.applicant_address == "г. Тест, ул. Ленина, 12, кв. 45"
    assert task.applicant_phone == "+7 999 123-45-67"
    assert task.description == "Течёт труба под раковиной"
    assert "создана и передана оператору" in await latest_text(db_session, resident_id)

    operator_card = await latest_text(db_session, 101)
    assert "Новая заявка от жителя" in operator_card
    assert "MAX ID: 303" in operator_card
    assert "ул. Ленина, 12" in operator_card
    dispatcher_card = await latest_text(db_session, 404)
    assert "Новая заявка от жителя" in dispatcher_card
    assert "MAX ID: 303" in dispatcher_card

    await send(client, event(resident_id, payload="resident_mine:0"))
    delivery = await db_session.scalar(
        select(BotDelivery)
        .where(BotDelivery.max_user_id == resident_id, BotDelivery.callback_id.is_(None))
        .order_by(BotDelivery.sequence.desc())
        .limit(1)
    )
    assert delivery and delivery.buttons
    assert f"№{task.number} · Принята" in delivery.buttons[0][0]["text"]

    await send(client, event(resident_id, payload=f"resident_task:{task.id}"))
    card = await latest_text(db_session, resident_id)
    assert f"Заявка №{task.number}" in card
    assert "Статус: Принята" in card
    assert "Течёт труба под раковиной" in card

    other_id = 304
    await send(client, event(other_id, text="/start"))
    await send(client, event(other_id, payload=f"resident_task:{task.id}"))
    assert "Заявка не найдена" in await latest_text(db_session, other_id)


async def test_resident_can_cancel_before_request_creation(
    client: AsyncClient,
    db_session: AsyncSession,
    bot_catalog: Organization,
) -> None:
    resident_id = 305
    await send(client, event(resident_id, payload="resident_new"))
    await send(client, event(resident_id, text="Иван"))
    await send(client, event(resident_id, text="Полный адрес 1"))
    await send(client, event(resident_id, text="Что-то сломалось"))
    await send(client, event(resident_id, text="+79990000000"))
    state = await db_session.get(BotConversation, resident_id)
    assert state and state.state == "resident_confirm"
    await send(client, event(resident_id, payload=f"resident_cancel:{state.data['flow']}"))
    assert "Заявка отменена" in await latest_text(db_session, resident_id)
    assert await db_session.scalar(select(func.count()).select_from(ServiceRequest)) == 0


async def test_worker_delivers_public_resident_reply(
    db_session: AsyncSession,
    test_settings: Settings,
    bot_catalog: Organization,
) -> None:
    resident = await resident_actor(db_session, test_settings, 306, "Житель")
    await reply(db_session, resident, "Ответ жителю")
    await db_session.commit()
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"message": {"body": {"mid": "resident-mid"}}})

    async with MaxMessagingClient(
        token=SecretStr("synthetic-token"), transport=httpx.MockTransport(transport)
    ) as max_client:
        assert await deliver_one(db_session, test_settings, max_client)

    assert requests
    row = await db_session.scalar(select(BotDelivery).where(BotDelivery.max_user_id == 306))
    assert row and row.state == "sent"


async def test_main_bot_filters_group_chatter_and_alerts_operator(
    client: AsyncClient,
    db_session: AsyncSession,
    bot_catalog: Organization,
) -> None:
    await send(client, event(101, text="/start"))
    before = await db_session.scalar(
        select(func.count()).select_from(BotDelivery).where(BotDelivery.max_user_id == 101)
    )

    await send(
        client,
        event(999, text="Кто сегодня смотрел футбол?", chat_type="chat", chat_id=-700),
    )
    after_chatter = await db_session.scalar(
        select(func.count()).select_from(BotDelivery).where(BotDelivery.max_user_id == 101)
    )
    assert after_chatter == before

    await send(
        client,
        event(
            999,
            text="Опять течёт труба в подъезде, вода уже на полу",
            chat_type="chat",
            chat_id=-700,
        ),
    )
    signal = await latest_text(db_session, 101)
    assert "Сигнал из чата" in signal
    assert "MAX ID: 999" in signal
    assert "течёт труба" in signal
    operator_signal = await latest_text(db_session, 404)
    assert "Сигнал из чата" in operator_signal
    assert "MAX ID: 999" in operator_signal
    assert await db_session.scalar(select(func.count()).select_from(ServiceRequest)) == 0


async def test_operator_dispatcher_menu_assignment_and_executor_isolation(
    client: AsyncClient,
    db_session: AsyncSession,
    bot_catalog: Organization,
) -> None:
    await send(client, event(404, text="/start"))
    operator_menu_text = await latest_text(db_session, 404)
    assert "Диспетчерская" in operator_menu_text
    assert "Новые: 0" in operator_menu_text
    assert "Без исполнителя: 0" in operator_menu_text

    await send(client, event(202, text="/start"))
    assert "Ваши задания" in await latest_text(db_session, 202)
    executor_delivery = await db_session.scalar(
        select(BotDelivery)
        .where(BotDelivery.max_user_id == 202, BotDelivery.callback_id.is_(None))
        .order_by(BotDelivery.sequence.desc())
        .limit(1)
    )
    assert executor_delivery and executor_delivery.buttons
    assert [item["text"] for row in executor_delivery.buttons for item in row] == ["Мои задания"]

    task = await create_task(client, db_session)
    await send(client, event(404, text="/menu"))
    operator_menu_text = await latest_text(db_session, 404)
    assert "Новые: 1" in operator_menu_text
    assert "Без исполнителя: 1" in operator_menu_text

    await send(client, event(404, payload="dispatch:unassigned:0"))
    queue_delivery = await db_session.scalar(
        select(BotDelivery)
        .where(BotDelivery.max_user_id == 404, BotDelivery.callback_id.is_(None))
        .order_by(BotDelivery.sequence.desc())
        .limit(1)
    )
    assert queue_delivery and queue_delivery.buttons
    assert queue_delivery.buttons[0][0]["payload"] == f"task:{task.id}"

    await send(client, event(404, payload="executors:0"))
    roster = await latest_text(db_session, 404)
    assert "Исполнители" in roster
    assert "Сотрудник 202" in roster
    assert "Сотрудник 404" not in roster

    employee = await db_session.scalar(select(Employee).where(Employee.max_user_id == 202))
    assert employee is not None
    await send(client, event(404, payload=f"assign:{task.id}:0:{employee.id}"))
    await db_session.refresh(task)
    assert task.assignee_id == employee.id and task.revision == 1
    assert "Исполнитель назначен" in await latest_text(db_session, 404)
    assert "Вам назначено задание" in await latest_text(db_session, 202)

    await send(client, event(404, text="/menu"))
    operator_menu_text = await latest_text(db_session, 404)
    assert "Без исполнителя: 0" in operator_menu_text

    await send(client, event(202, payload="dispatch:all:0"))
    assert "Действие недоступно" in await latest_text(db_session, 202)
    denied_delivery = await db_session.scalar(
        select(BotDelivery)
        .where(BotDelivery.max_user_id == 202, BotDelivery.callback_id.is_(None))
        .order_by(BotDelivery.sequence.desc())
        .limit(1)
    )
    assert denied_delivery and denied_delivery.buttons
    assert [item["text"] for row in denied_delivery.buttons for item in row] == ["Мои задания"]


async def test_owner_manages_staff_roles_inside_bot(
    client: AsyncClient,
    db_session: AsyncSession,
    bot_catalog: Organization,
) -> None:
    await send(client, event(101, text="/start"))
    owner_menu = await db_session.scalar(
        select(BotDelivery)
        .where(BotDelivery.max_user_id == 101, BotDelivery.callback_id.is_(None))
        .order_by(BotDelivery.sequence.desc())
        .limit(1)
    )
    assert owner_menu and owner_menu.buttons
    assert "Управление" in [item["text"] for row in owner_menu.buttons for item in row]

    await send(client, event(404, text="/start"))
    operator_menu = await db_session.scalar(
        select(BotDelivery)
        .where(BotDelivery.max_user_id == 404, BotDelivery.callback_id.is_(None))
        .order_by(BotDelivery.sequence.desc())
        .limit(1)
    )
    assert operator_menu and operator_menu.buttons
    assert "Управление" not in [item["text"] for row in operator_menu.buttons for item in row]

    await send(client, event(101, payload="admin"))
    assert "Управление" in await latest_text(db_session, 101)
    await send(client, event(101, payload="admin_staff_add"))
    assert "MAX ID сотрудника" in await latest_text(db_session, 101)
    await send(client, event(101, text="505"))
    assert "Какую роль выдать" in await latest_text(db_session, 101)
    await send(client, event(101, payload="admin_staff_add_role:executor"))

    employee = await db_session.scalar(
        select(Employee).where(
            Employee.organization_id == bot_catalog.id,
            Employee.max_user_id == 505,
        )
    )
    assert employee is not None and employee.is_active
    role = await db_session.get(Role, employee.role_id)
    assert role is not None and role.name == "Сотрудник бота"

    await send(client, event(505, text="/start"))
    assert "Ваши задания" in await latest_text(db_session, 505)

    await send(client, event(101, payload=f"admin_staff_role:{employee.id}:operator"))
    await db_session.refresh(employee)
    role = await db_session.get(Role, employee.role_id)
    assert role is not None and role.name == "Оператор"

    await send(client, event(505, text="/menu"))
    assert "Диспетчерская" in await latest_text(db_session, 505)

    before = await db_session.scalar(
        select(func.count()).select_from(BotDelivery).where(BotDelivery.max_user_id == 505)
    )
    await send(client, event(101, payload=f"admin_staff_active:{employee.id}:0"))
    await db_session.refresh(employee)
    assert not employee.is_active

    await send(client, event(505, text="/start"))
    after = await db_session.scalar(
        select(func.count()).select_from(BotDelivery).where(BotDelivery.max_user_id == 505)
    )
    assert after == before

    owner_employee = await db_session.scalar(
        select(Employee).where(
            Employee.organization_id == bot_catalog.id,
            Employee.max_user_id == 101,
        )
    )
    assert owner_employee is not None
    await send(client, event(101, payload=f"admin_staff_card:{owner_employee.id}"))
    protected_card = await db_session.scalar(
        select(BotDelivery)
        .where(BotDelivery.max_user_id == 101, BotDelivery.callback_id.is_(None))
        .order_by(BotDelivery.sequence.desc())
        .limit(1)
    )
    assert protected_card and protected_card.buttons
    labels = [item["text"] for row in protected_card.buttons for item in row]
    assert "Сделать исполнителем" not in labels
    assert "Отключить" not in labels


async def test_owner_controls_group_chat_analysis(
    client: AsyncClient,
    db_session: AsyncSession,
    bot_catalog: Organization,
    test_settings: Settings,
) -> None:
    await send(
        client,
        event(999, text="Обычный разговор без проблемы", chat_type="chat", chat_id=-800),
    )
    chat = await db_session.get(BotGroupChat, -800)
    assert chat is not None
    assert chat.organization_id == bot_catalog.id and chat.analysis_enabled

    await send(client, event(101, payload="admin_chats:0"))
    assert "Групповые чаты" in await latest_text(db_session, 101)

    await send(client, event(101, payload="admin_chat_toggle:-800:0"))
    await db_session.refresh(chat)
    assert not chat.analysis_enabled

    owner_before = await db_session.scalar(
        select(func.count()).select_from(BotDelivery).where(BotDelivery.max_user_id == 101)
    )
    operator_before = await db_session.scalar(
        select(func.count()).select_from(BotDelivery).where(BotDelivery.max_user_id == 404)
    )
    await send(
        client,
        event(999, text="Течёт труба, затапливает этаж", chat_type="chat", chat_id=-800),
    )
    assert (
        await db_session.scalar(
            select(func.count()).select_from(BotDelivery).where(BotDelivery.max_user_id == 101)
        )
        == owner_before
    )
    assert (
        await db_session.scalar(
            select(func.count()).select_from(BotDelivery).where(BotDelivery.max_user_id == 404)
        )
        == operator_before
    )

    await send(client, event(101, payload="admin_settings_group:0"))
    bot_settings = await db_session.get(BotOrganizationSettings, bot_catalog.id)
    assert bot_settings is not None and not bot_settings.group_analysis_enabled

    owner_before = await db_session.scalar(
        select(func.count()).select_from(BotDelivery).where(BotDelivery.max_user_id == 101)
    )
    await send(
        client,
        event(999, text="Нет воды и прорвало стояк", chat_type="chat", chat_id=-801),
    )
    second_chat = await db_session.get(BotGroupChat, -801)
    assert second_chat is not None and second_chat.analysis_enabled
    assert (
        await db_session.scalar(
            select(func.count()).select_from(BotDelivery).where(BotDelivery.max_user_id == 101)
        )
        == owner_before
    )

    await send(client, event(101, payload="admin_settings"))
    settings_text = await latest_text(db_session, 101)
    assert "Токены и секреты меняются только на сервере" in settings_text
    assert test_settings.max_staff_token is not None
    assert test_settings.max_staff_token.get_secret_value() not in settings_text


async def test_non_owner_cannot_open_owner_admin(
    client: AsyncClient,
    db_session: AsyncSession,
    bot_catalog: Organization,
) -> None:
    await send(client, event(404, payload="admin"))
    assert "Действие недоступно" in await latest_text(db_session, 404)
    delivery = await db_session.scalar(
        select(BotDelivery)
        .where(BotDelivery.max_user_id == 404, BotDelivery.callback_id.is_(None))
        .order_by(BotDelivery.sequence.desc())
        .limit(1)
    )
    assert delivery and delivery.buttons
    assert "Управление" not in [item["text"] for row in delivery.buttons for item in row]
