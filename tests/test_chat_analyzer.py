"""Asynchronous DeepSeek chat analyzer retries and emergency fallback."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.admin import ensure_group_chat, get_bot_settings
from app.bot.chat_analyzer import RETRY_DELAY, analyze_one
from app.bot.chat_signals import enqueue_chat_analysis
from app.core.config import Settings
from app.integrations.deepseek.client import DeepSeekFailure, DeepSeekResult
from app.models.bot import BotDelivery, ChatAnalysisJob, ChatSignal
from app.models.crm import Organization


class TimeoutClassifier:
    async def classify(self, text: str) -> DeepSeekResult:
        raise DeepSeekFailure("timeout")


async def _queue(
    db: AsyncSession,
    organization_id,
    *,
    event_key: str,
    text: str,
    actor_id: int = 999,
    chat_id: int = -900,
) -> ChatAnalysisJob:
    await ensure_group_chat(db, organization_id, chat_id)
    await get_bot_settings(db, organization_id)
    job = await enqueue_chat_analysis(
        db,
        event_key=event_key,
        chat_id=chat_id,
        actor_max_user_id=actor_id,
        text=text,
    )
    await db.commit()
    return job


async def test_nonurgent_deepseek_failure_retries_three_times_then_scrubs(
    db_session: AsyncSession,
    test_settings: Settings,
    bot_catalog: Organization,
) -> None:
    job = await _queue(
        db_session,
        bot_catalog.id,
        event_key="a" * 64,
        text="Лифт не работает второй час",
    )
    classifier = TimeoutClassifier()

    assert await analyze_one(db_session, test_settings, classifier)
    await db_session.refresh(job)
    assert job.state == "pending"
    assert job.attempts == 1
    assert job.error_code == "timeout"
    assert job.text

    for expected_attempt in (2, 3):
        job.last_attempt_at = datetime.now(UTC) - RETRY_DELAY - timedelta(seconds=1)
        await db_session.commit()
        assert await analyze_one(db_session, test_settings, classifier)
        await db_session.refresh(job)
        assert job.attempts == expected_attempt

    assert job.state == "failed"
    assert job.error_code == "timeout"
    assert job.text == ""
    assert await db_session.scalar(select(func.count()).select_from(ChatSignal)) == 0


async def test_urgent_problem_uses_fallback_when_deepseek_times_out(
    db_session: AsyncSession,
    test_settings: Settings,
    bot_catalog: Organization,
) -> None:
    job = await _queue(
        db_session,
        bot_catalog.id,
        event_key="b" * 64,
        text="В подъезде пахнет газом",
        chat_id=-901,
    )

    assert await analyze_one(db_session, test_settings, TimeoutClassifier())
    await db_session.refresh(job)
    assert job.state == "done"
    assert job.attempts == 1
    assert job.error_code == "timeout"
    assert job.text == ""

    signal = await db_session.scalar(
        select(ChatSignal).where(ChatSignal.event_key == "b" * 64)
    )
    assert signal is not None
    assert signal.source == "urgent_fallback"
    assert signal.severity == "urgent"
    assert signal.confidence is None

    owner_text = await db_session.scalar(
        select(BotDelivery.text)
        .where(BotDelivery.max_user_id == 101, BotDelivery.callback_id.is_(None))
        .order_by(BotDelivery.sequence.desc())
        .limit(1)
    )
    operator_text = await db_session.scalar(
        select(BotDelivery.text)
        .where(BotDelivery.max_user_id == 404, BotDelivery.callback_id.is_(None))
        .order_by(BotDelivery.sequence.desc())
        .limit(1)
    )
    assert owner_text is not None and "пахнет газом" in owner_text.lower()
    assert operator_text is not None and "пахнет газом" in operator_text.lower()
