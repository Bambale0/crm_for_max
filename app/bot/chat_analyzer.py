"""Analyze queued group messages with DeepSeek V4 Flash outside the MAX webhook."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import InvalidCredentials
from app.bot.admin import dispatcher_max_ids
from app.bot.chat_signals import (
    DeepSeekLike,
    problem_from_deepseek_result,
    record_signal_if_fresh,
    urgent_fallback,
)
from app.bot.identity import native_actor
from app.bot.ui import reply
from app.core.config import Settings
from app.core.database import Database
from app.core.logging import configure_logging
from app.integrations.deepseek.client import DeepSeekClassifier, DeepSeekFailure
from app.models.bot import BotGroupChat, BotOrganizationSettings, ChatAnalysisJob

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
RETRY_DELAY = timedelta(seconds=15)
STALE_PROCESSING = timedelta(minutes=5)


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


async def _locked_job(db: AsyncSession, job_id) -> ChatAnalysisJob | None:
    return await db.scalar(
        select(ChatAnalysisJob)
        .where(ChatAnalysisJob.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def analyze_one(
    db: AsyncSession,
    settings: Settings,
    classifier: DeepSeekLike,
) -> bool:
    now = datetime.now(UTC)
    await db.execute(
        update(ChatAnalysisJob)
        .where(
            ChatAnalysisJob.state == "processing",
            ChatAnalysisJob.last_attempt_at < now - STALE_PROCESSING,
        )
        .values(state="pending", error_code="interrupted")
    )

    job = await db.scalar(
        select(ChatAnalysisJob)
        .where(
            ChatAnalysisJob.state == "pending",
            ChatAnalysisJob.attempts < MAX_ATTEMPTS,
            or_(
                ChatAnalysisJob.last_attempt_at.is_(None),
                ChatAnalysisJob.last_attempt_at <= now - RETRY_DELAY,
            ),
        )
        .order_by(ChatAnalysisJob.created_at, ChatAnalysisJob.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        await db.commit()
        return False

    chat = await db.get(BotGroupChat, job.chat_id, populate_existing=True)
    bot_settings = (
        await db.get(BotOrganizationSettings, chat.organization_id, populate_existing=True)
        if chat is not None
        else None
    )
    if (
        chat is None
        or bot_settings is None
        or not chat.analysis_enabled
        or not bot_settings.group_analysis_enabled
    ):
        job.state = "discarded"
        job.error_code = "analysis_disabled"
        job.text = ""
        await db.commit()
        return True

    job.state = "processing"
    job.attempts += 1
    job.last_attempt_at = now
    text_to_analyze = job.text
    job_id = job.id
    await db.commit()

    try:
        result = await classifier.classify(text_to_analyze)
        classified = problem_from_deepseek_result(
            text_to_analyze,
            result,
            min_confidence=settings.deepseek_min_confidence,
        )
        failure: DeepSeekFailure | None = None
    except DeepSeekFailure as error:
        classified = urgent_fallback(text_to_analyze)
        failure = error
        logger.warning(
            "deepseek_chat_analysis_failed job_id=%s code=%s",
            job_id,
            error.code,
        )

    job = await _locked_job(db, job_id)
    if job is None:
        await db.rollback()
        return True
    if job.state != "processing":
        await db.commit()
        return True

    if failure is not None and classified is None and job.attempts < MAX_ATTEMPTS:
        job.state = "pending"
        job.error_code = failure.code
        await db.commit()
        return True

    if classified is not None:
        signal = await record_signal_if_fresh(
            db,
            chat_id=job.chat_id,
            actor_max_user_id=job.actor_max_user_id,
            event_key=job.event_key,
            classified=classified,
        )
        if signal is not None:
            await notify_group_problem(
                db,
                settings,
                signal.actor_max_user_id,
                signal.problem,
            )

    job.state = "done" if failure is None or classified is not None else "failed"
    job.error_code = failure.code if failure is not None else None
    job.text = ""
    await db.commit()
    return True


async def run() -> None:
    configure_logging()
    settings = Settings()
    if settings.deepseek_api_key is None:
        raise SystemExit("DEEPSEEK_API_KEY is required for the chat analyzer")
    database = Database(settings.database_url.get_secret_value())
    try:
        async with DeepSeekClassifier(
            settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            timeout_seconds=settings.deepseek_timeout_seconds,
        ) as classifier:
            while True:
                async with database.session_factory() as db:
                    processed = await analyze_one(db, settings, classifier)
                await asyncio.sleep(0.2 if processed else 1.0)
    finally:
        await database.close()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except Exception as error:
        raise SystemExit(
            f"Chat analyzer stopped ({type(error).__name__}); check service health"
        ) from None
