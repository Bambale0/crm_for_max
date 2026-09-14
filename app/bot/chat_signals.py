"""DeepSeek-first group-chat classification with urgent local fallback and flood protection."""

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.deepseek.client import DeepSeekFailure, DeepSeekResult
from app.models.bot import BotGroupChat, ChatAnalysisJob, ChatSignal

logger = logging.getLogger(__name__)

URGENT_MARKERS = (
    "пожар",
    "дым",
    "запах газа",
    "пахнет газом",
    "газом пахнет",
    "искрит",
    "коротит",
    "короткое замыкание",
    "прорвало",
    "прорыв",
    "затапливает",
    "затопило",
    "застряли в лифте",
    "застрял в лифте",
    "застряла в лифте",
)


class DeepSeekLike(Protocol):
    async def classify(self, text: str) -> DeepSeekResult: ...


@dataclass(frozen=True)
class GroupProblem:
    problem: str
    fingerprint: str
    severity: str
    source: str
    confidence: float | None


def normalize_problem_text(text: str) -> str:
    return " ".join(text.strip().split())[:3400]


def _searchable(text: str) -> str:
    lowered = text.casefold().replace("ё", "е")
    lowered = re.sub(r"[^\w\s]+", " ", lowered)
    return " ".join(lowered.split())


def _fingerprint(text: str) -> str:
    return hashlib.sha256(_searchable(text).encode()).hexdigest()


def urgent_fallback(text: str) -> GroupProblem | None:
    problem = normalize_problem_text(text)
    searchable = _searchable(problem)
    if len(searchable) < 6:
        return None
    if not any(marker.replace("ё", "е") in searchable for marker in URGENT_MARKERS):
        return None
    return GroupProblem(
        problem=problem,
        fingerprint=_fingerprint(problem),
        severity="urgent",
        source="urgent_fallback",
        confidence=None,
    )


def problem_from_deepseek_result(
    text: str,
    result: DeepSeekResult,
    *,
    min_confidence: float,
) -> GroupProblem | None:
    original = normalize_problem_text(text)
    fallback = urgent_fallback(original)
    if not result.is_problem or result.confidence < min_confidence:
        return fallback
    return GroupProblem(
        problem=result.problem[:1000],
        fingerprint=_fingerprint(original),
        severity=result.severity,
        source="deepseek",
        confidence=result.confidence,
    )


async def classify_group_message(
    classifier: DeepSeekLike | None,
    text: str,
    *,
    min_confidence: float,
) -> GroupProblem | None:
    original = normalize_problem_text(text)
    if len(_searchable(original)) < 4:
        return None
    if classifier is None:
        return urgent_fallback(original)
    try:
        result = await classifier.classify(original)
    except DeepSeekFailure as error:
        logger.warning("deepseek_group_classification_failed code=%s", error.code)
        return urgent_fallback(original)
    return problem_from_deepseek_result(
        original,
        result,
        min_confidence=min_confidence,
    )


async def enqueue_chat_analysis(
    db: AsyncSession,
    *,
    event_key: str,
    chat_id: int,
    actor_max_user_id: int,
    text: str,
) -> ChatAnalysisJob:
    job = ChatAnalysisJob(
        event_key=event_key,
        chat_id=chat_id,
        actor_max_user_id=actor_max_user_id,
        text=normalize_problem_text(text)[:4000],
    )
    db.add(job)
    await db.flush()
    return job


async def record_signal_if_fresh(
    db: AsyncSession,
    *,
    chat_id: int,
    actor_max_user_id: int,
    event_key: str,
    classified: GroupProblem,
) -> ChatSignal | None:
    chat = await db.scalar(
        select(BotGroupChat).where(BotGroupChat.chat_id == chat_id).with_for_update()
    )
    if chat is None:
        return None

    now = datetime.now(UTC)
    duplicate = await db.scalar(
        select(ChatSignal.id).where(
            ChatSignal.chat_id == chat_id,
            ChatSignal.actor_max_user_id == actor_max_user_id,
            ChatSignal.fingerprint == classified.fingerprint,
            ChatSignal.created_at >= now - timedelta(minutes=30),
        )
    )
    if duplicate is not None:
        return None

    if classified.severity != "urgent":
        recent = await db.scalar(
            select(ChatSignal.id)
            .where(
                ChatSignal.chat_id == chat_id,
                ChatSignal.actor_max_user_id == actor_max_user_id,
                ChatSignal.created_at >= now - timedelta(minutes=2),
            )
            .order_by(ChatSignal.created_at.desc())
            .limit(1)
        )
        if recent is not None:
            return None

    signal = ChatSignal(
        event_key=event_key,
        chat_id=chat_id,
        actor_max_user_id=actor_max_user_id,
        fingerprint=classified.fingerprint,
        severity=classified.severity,
        source=classified.source,
        confidence=classified.confidence,
        problem=classified.problem,
    )
    db.add(signal)
    await db.flush()
    return signal
