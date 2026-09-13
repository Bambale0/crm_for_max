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
from app.bot.dialogs import reply
from app.bot.identity import native_actor
from app.bot.setup import bind_chat, initialize
from app.bot.worker import deliver_one
from app.core.config import Settings
from app.integrations.max.messaging import MaxMessagingClient
from app.main import create_app
from app.models.base import Base
from app.models.bot import BotConversation, BotDelivery, BotReceipt, ChatObservation
from app.models.crm import Category, Employee, House, Organization, RequestStatus, ServiceRequest
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
    assert "Вам назначена" in await latest_text(db_session, 202)
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
    await send(client, event(999, text="/start"))
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


@pytest.mark.parametrize("revocation", ["env", "scope"])
async def test_queued_private_content_rechecks_current_access(
    db_session: AsyncSession,
    test_settings: Settings,
    bot_catalog: Organization,
    revocation: str,
) -> None:
    actor = await native_actor(db_session, test_settings, 202, "Исполнитель")
    await reply(db_session, actor, "Приватная карточка")
    if revocation == "env":
        test_settings.max_employee_ids = ()
    else:
        employee = await db_session.scalar(select(Employee).where(Employee.max_user_id == 202))
        assert employee
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
    await initialize(
        db_session, test_settings, "Не переименовывать", ["Тестовый дом 1"], bot_catalog.id
    )
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
            assert await verify.scalar(select(func.count()).select_from(BotDelivery)) == 2
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
