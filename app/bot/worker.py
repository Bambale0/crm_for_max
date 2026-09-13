"""Deliver committed personal replies once; ambiguous sends are never retried."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import InvalidCredentials
from app.bot.identity import access_stamp, native_actor
from app.core.config import Settings
from app.core.database import Database
from app.core.logging import configure_logging
from app.integrations.max.messaging import CallbackButton, MaxDeliveryError, MaxMessagingClient
from app.models.bot import BotDelivery

logger = logging.getLogger(__name__)
# One sender per database keeps the simple global throttle below MAX per-dialog limits.
WORKER_LOCK_ID = 7780021


async def deliver_one(db: AsyncSession, settings: Settings, client: MaxMessagingClient) -> bool:
    now = datetime.now(UTC)
    # A process can die after MAX accepted a message but before its response was saved.
    await db.execute(
        update(BotDelivery)
        .where(
            BotDelivery.state == "sending",
            BotDelivery.attempted_at < now - timedelta(minutes=2),
        )
        .values(
            state="uncertain", error_code="interrupted", text="", buttons=None, callback_id=None
        )
    )
    delivery = await db.scalar(
        select(BotDelivery)
        .where(BotDelivery.state == "pending")
        .order_by(BotDelivery.sequence)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if delivery is None:
        await db.commit()
        return False
    try:
        actor = await native_actor(db, settings, delivery.max_user_id)
        valid = delivery.access_stamp == await access_stamp(db, actor)
    except InvalidCredentials:
        valid = False
    if not valid or delivery.created_at < now - timedelta(minutes=30):
        delivery.state, delivery.error_code = (
            "discarded",
            "access_changed" if not valid else "expired",
        )
        delivery.text, delivery.buttons, delivery.callback_id = "", None, None
        await db.commit()
        return True
    delivery.state, delivery.attempted_at = "sending", now
    await db.commit()
    try:
        if delivery.callback_id:
            await client.answer_callback(delivery.callback_id)
        else:
            buttons = (
                [[CallbackButton(**item) for item in row] for row in delivery.buttons]
                if delivery.buttons
                else None
            )
            sent = await client.send_text(delivery.max_user_id, delivery.text, buttons)
            delivery.message_id = sent.message_id
        delivery.state = "sent"
    except MaxDeliveryError as error:
        delivery.state = "uncertain" if error.delivery_uncertain else "failed"
        delivery.error_code = error.code
        logger.warning(
            "max_delivery_failed delivery_id=%s code=%s uncertain=%s",
            delivery.id,
            error.code,
            error.delivery_uncertain,
        )
    delivery.text, delivery.buttons, delivery.callback_id = "", None, None
    await db.commit()
    return True


async def run() -> None:
    configure_logging()
    settings = Settings()
    if settings.max_staff_token is None:
        raise SystemExit("MAX_STAFF_TOKEN is required for the bot worker")
    database = Database(settings.database_url.get_secret_value())
    try:
        async with database.engine.connect() as lease:
            locked = await lease.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": WORKER_LOCK_ID}
            )
            await lease.commit()
            if not locked:
                raise SystemExit("A bot worker is already running for this database")
            async with MaxMessagingClient(
                token=settings.max_staff_token,
                base_url=settings.max_api_base_url,
                timeout_seconds=settings.max_timeout_seconds,
            ) as client:
                while True:
                    async with database.session_factory() as db:
                        await deliver_one(db, settings, client)
                    await asyncio.sleep(0.6)
    finally:
        await database.close()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except Exception as error:
        raise SystemExit(
            f"Bot worker stopped ({type(error).__name__}); check service health"
        ) from None
