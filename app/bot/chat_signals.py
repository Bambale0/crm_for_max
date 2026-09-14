"""Conservative local classifier and flood protection for group-chat problems."""

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bot import BotGroupChat, ChatSignal

URGENT_MARKERS = (
    "пожар",
    "дым",
    "запах газа",
    "пахнет газом",
    "газом пахнет",
    "искрит",
    "коротит",
    "прорвало",
    "прорыв",
    "затапливает",
    "затопило",
    "авария",
    "аварий",
)

RESOLVED_MARKERS = (
    "починили",
    "исправили",
    "устранили",
    "заработал",
    "заработала",
    "заработало",
    "уже работает",
    "все работает",
    "всё работает",
    "все нормально",
    "всё нормально",
    "не течет",
    "не течёт",
    "вода есть",
    "свет есть",
)

TARGETS_GENERIC = (
    "лифт",
    "домофон",
    "освещ",
    "свет",
    "двер",
    "замок",
    "труба",
    "стояк",
    "кран",
    "насос",
    "вентиляц",
)

BROKEN_MARKERS = (
    "сломался",
    "сломалась",
    "сломалось",
    "сломано",
    "сломали",
    "не работает",
    "не включается",
    "не открывается",
    "не закрывается",
    "не едет",
    "застрял",
    "застряла",
    "застряли",
)

WATER_OUTAGE_MARKERS = (
    "нет воды",
    "без воды",
    "нет горячей воды",
    "нет холодной воды",
    "горячей воды нет",
    "холодной воды нет",
    "отключили воду",
)

WATER_LEAK_MARKERS = (
    "течет",
    "течёт",
    "протекает",
    "протекло",
    "капает",
)

WATER_TARGETS = (
    "труб",
    "кран",
    "стояк",
    "потол",
    "крыша",
    "радиатор",
    "батар",
    "счетчик",
    "счётчик",
    "подъезд",
    "вода",
    "унитаз",
    "раковин",
)

POWER_MARKERS = (
    "нет света",
    "света нет",
    "нет электричества",
    "электричества нет",
    "выбило свет",
    "не горит освещение",
)

SEWER_PROBLEM_MARKERS = (
    "засор",
    "не уходит вода",
)

SEWER_TARGETS = ("канализац",)
SEWER_MARKERS = ("вон", "запах", "теч", "не работает")

TRASH_TARGETS = ("мусор", "контейнер", "бак", "помой")
TRASH_MARKERS = ("не вывез", "не вывоз", "переполн", "завален", "лежит", "уберите")

HEATING_TARGETS = ("батар", "отоплен", "радиатор")
HEATING_MARKERS = ("холод", "не гре", "нет отопления")


@dataclass(frozen=True)
class GroupProblem:
    problem: str
    fingerprint: str
    severity: str


def normalize_problem_text(text: str) -> str:
    normalized = " ".join(text.strip().split())
    return normalized[:3400]


def _searchable(text: str) -> str:
    lowered = text.casefold().replace("ё", "е")
    lowered = re.sub(r"[^\w\s]+", " ", lowered)
    return " ".join(lowered.split())


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker.replace("ё", "е") in text for marker in markers)


def classify_group_problem(text: str) -> GroupProblem | None:
    problem = normalize_problem_text(text)
    if len(problem) < 6:
        return None
    searchable = _searchable(problem)
    if len(searchable) < 6:
        return None

    urgent = _contains_any(searchable, URGENT_MARKERS)
    resolved = _contains_any(searchable, RESOLVED_MARKERS)
    contrast = any(
        marker in f" {searchable} "
        for marker in (" но ", " однако ", " зато ", " при этом ")
    )
    if not urgent and resolved and not contrast:
        return None

    is_problem = urgent
    is_problem = is_problem or _contains_any(searchable, WATER_OUTAGE_MARKERS)
    is_problem = is_problem or (
        _contains_any(searchable, WATER_LEAK_MARKERS) and _contains_any(searchable, WATER_TARGETS)
    )
    is_problem = is_problem or _contains_any(searchable, POWER_MARKERS)
    is_problem = is_problem or _contains_any(searchable, SEWER_PROBLEM_MARKERS)
    is_problem = is_problem or (
        _contains_any(searchable, SEWER_TARGETS) and _contains_any(searchable, SEWER_MARKERS)
    )
    is_problem = is_problem or (
        _contains_any(searchable, TARGETS_GENERIC) and _contains_any(searchable, BROKEN_MARKERS)
    )
    is_problem = is_problem or (
        _contains_any(searchable, TRASH_TARGETS) and _contains_any(searchable, TRASH_MARKERS)
    )
    is_problem = is_problem or (
        _contains_any(searchable, HEATING_TARGETS) and _contains_any(searchable, HEATING_MARKERS)
    )
    if not is_problem:
        return None

    fingerprint = hashlib.sha256(searchable.encode()).hexdigest()
    return GroupProblem(
        problem=problem,
        fingerprint=fingerprint,
        severity="urgent" if urgent else "normal",
    )


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
        problem=classified.problem,
    )
    db.add(signal)
    await db.flush()
    return signal
