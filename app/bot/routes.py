"""Secret-verified MAX callbacks: acknowledge only after an atomic database commit."""

import hmac
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_settings
from app.auth.service import InvalidCredentials
from app.bot.admin import (
    dispatcher_max_ids,
    ensure_group_chat,
    get_bot_settings,
)
from app.bot.chat_signals import classify_group_problem, record_signal_if_fresh
from app.bot.dialogs import conversation, handle_staff, staff_menu
from app.bot.identity import native_actor, resident_actor
from app.bot.resident import handle_resident, resident_menu
from app.bot.ui import reply
from app.bot.updates import normalize_update
from app.core.config import Settings
from app.crm.errors import CRMConflict, CRMError
from app.models.bot import BotReceipt, ChatObservation, HouseChat

router = APIRouter(prefix="/api/bots/max", tags=["MAX bots"])
MAX_UPDATE_BYTES = 256 * 1024
IMPORTANT_WORDS = ("пожар", "дым", "запах газа", "прорвало", "затоп", "искрит", "авари")


async def notify_group_problem(
    db: AsyncSession,
    settings: Settings,
    max_user_id: int,
    problem: str,
) -> None:
    organization_id = settings.max_bot_organization_id
    if organization_id is None:
        return
    for operator_id in await dispatcher_max_ids(db, organization_id, settings.max_owner_ids):
        try:
            operator = await native_actor(db, settings, operator_id)
        except InvalidCredentials:
            continue
        await reply(
            db,
            operator,
            f"Сигнал из чата\nMAX ID: {max_user_id}\nПроблема: {problem[:3400]}",
        )


@router.post("/{namespace}")
async def max_webhook(
    namespace: Literal["staff", "observer"],
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, bool]:
    secret = (
        settings.max_staff_webhook_secret
        if namespace == "staff"
        else settings.max_observer_webhook_secret
    )
    token = settings.max_staff_token if namespace == "staff" else settings.max_observer_token
    if secret is None or token is None:
        raise HTTPException(503, "Bot is not configured")
    supplied = request.headers.get("X-Max-Bot-Api-Secret", "")
    if not hmac.compare_digest(supplied.encode(), secret.get_secret_value().encode()):
        raise HTTPException(403, "Invalid webhook secret")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_UPDATE_BYTES:
            raise HTTPException(413, "Update is too large")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Invalid JSON") from None
    update = normalize_update(namespace, payload)
    if update is None:
        return {"ok": True}
    if namespace == "staff" and not update.is_private:
        if update.update_type != "message_created" or not update.text:
            return {"ok": True}
    if namespace == "observer":
        if update.is_private or update.update_type != "message_created" or not update.text:
            return {"ok": True}
        binding = await db.get(HouseChat, update.chat_id)
        if binding is None:
            return {"ok": True}

    receipt = await db.scalar(
        insert(BotReceipt)
        .values(event_key=update.event_key)
        .on_conflict_do_nothing()
        .returning(BotReceipt.event_key)
    )
    if receipt is None:
        await db.commit()
        return {"ok": True}

    if namespace == "staff" and not update.is_private:
        assert update.text is not None
        organization_id = settings.max_bot_organization_id
        if organization_id is not None:
            chat = await ensure_group_chat(db, organization_id, update.chat_id)
            bot_settings = await get_bot_settings(db, organization_id)
            if bot_settings.group_analysis_enabled and chat.analysis_enabled:
                classified = classify_group_problem(update.text)
                if classified is not None:
                    signal = await record_signal_if_fresh(
                        db,
                        chat_id=update.chat_id,
                        actor_max_user_id=update.actor_id,
                        event_key=update.event_key,
                        classified=classified,
                    )
                    if signal is not None:
                        await notify_group_problem(
                            db,
                            settings,
                            signal.actor_max_user_id,
                            signal.problem,
                        )
        await db.commit()
        return {"ok": True}

    if namespace == "observer":
        assert update.text is not None
        db.add(
            ChatObservation(
                event_key=update.event_key,
                chat_id=update.chat_id,
                text=update.text,
                important=any(word in update.text.casefold() for word in IMPORTANT_WORDS),
            )
        )
    else:
        is_staff = True
        try:
            actor = await native_actor(db, settings, update.actor_id, update.actor_name)
        except InvalidCredentials:
            is_staff = False
            try:
                actor = await resident_actor(db, settings, update.actor_id, update.actor_name)
            except InvalidCredentials:
                await db.commit()
                return {"ok": True}

        state = await conversation(db, update)
        if update.callback_id:
            await reply(db, actor, "", callback_id=update.callback_id)

        if is_staff and settings.max_bot_organization_id is not None:
            buttons = await staff_menu(db, actor, settings.max_bot_organization_id)
        else:
            buttons = resident_menu()
        if update.timestamp_ms >= state.last_timestamp_ms:
            state.last_timestamp_ms = update.timestamp_ms
            try:
                async with db.begin_nested():
                    if is_staff:
                        await handle_staff(db, settings, update, actor, state)
                    else:
                        await handle_resident(db, settings, update, actor, state)
            except CRMConflict:
                await reply(
                    db,
                    actor,
                    (
                        "Заявка или диалог уже изменились. Откройте актуальную карточку из списка."
                        if is_staff
                        else "Форма уже изменилась. Откройте меню и начните заново."
                    ),
                    buttons,
                )
            except (CRMError, ValueError, KeyError, ValidationError):
                await reply(
                    db,
                    actor,
                    (
                        "Действие недоступно. Проверьте права или откройте заявку заново."
                        if is_staff
                        else "Не удалось выполнить действие. Откройте меню и попробуйте ещё раз."
                    ),
                    buttons,
                )
        else:
            await reply(
                db,
                actor,
                (
                    "Пришло старое сообщение. Откройте актуальную карточку из списка."
                    if is_staff
                    else "Это сообщение уже неактуально. Откройте меню."
                ),
                buttons,
            )
    await db.commit()
    return {"ok": True}
