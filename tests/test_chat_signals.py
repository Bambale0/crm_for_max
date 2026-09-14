"""DeepSeek-first group-chat classification without MAX or database dependencies."""

from dataclasses import dataclass

import pytest

from app.bot.chat_signals import classify_group_message
from app.integrations.deepseek.client import DeepSeekFailure, DeepSeekResult


@dataclass
class StubClassifier:
    result: DeepSeekResult | None = None
    failure: DeepSeekFailure | None = None

    async def classify(self, text: str) -> DeepSeekResult:
        if self.failure is not None:
            raise self.failure
        assert self.result is not None
        return self.result


async def test_deepseek_is_primary_problem_classifier() -> None:
    classifier = StubClassifier(
        result=DeepSeekResult(
            is_problem=True,
            problem="Не работает лифт.",
            confidence=0.94,
            severity="normal",
        )
    )
    result = await classify_group_message(
        classifier,
        "Лифт второй час стоит, кнопки не реагируют",
        min_confidence=0.72,
    )
    assert result is not None
    assert result.problem == "Не работает лифт."
    assert result.severity == "normal"
    assert result.source == "deepseek"
    assert result.confidence == 0.94


async def test_deepseek_can_reject_chatter() -> None:
    classifier = StubClassifier(
        result=DeepSeekResult(
            is_problem=False,
            problem="ignored by validation",
            confidence=0.98,
            severity="urgent",
        )
    )
    result = await classify_group_message(
        classifier,
        "Кто сегодня смотрел футбол?",
        min_confidence=0.72,
    )
    assert result is None


async def test_low_confidence_nonurgent_result_is_discarded() -> None:
    classifier = StubClassifier(
        result=DeepSeekResult(
            is_problem=True,
            problem="Возможно, не работает домофон.",
            confidence=0.51,
            severity="normal",
        )
    )
    result = await classify_group_message(
        classifier,
        "Домофон что-то сегодня странно себя ведёт",
        min_confidence=0.72,
    )
    assert result is None


@pytest.mark.parametrize(
    "message",
    [
        "В подъезде пахнет газом",
        "В щитке искрит",
        "Прорвало стояк, затапливает этаж",
        "Дым идёт из электрощитовой",
    ],
)
async def test_urgent_local_fallback_survives_deepseek_failure(message: str) -> None:
    classifier = StubClassifier(failure=DeepSeekFailure("timeout"))
    result = await classify_group_message(classifier, message, min_confidence=0.72)
    assert result is not None
    assert result.problem == message
    assert result.severity == "urgent"
    assert result.source == "urgent_fallback"
    assert result.confidence is None


async def test_urgent_fallback_overrides_false_negative() -> None:
    classifier = StubClassifier(
        result=DeepSeekResult(
            is_problem=False,
            problem="",
            confidence=0.99,
            severity="normal",
        )
    )
    result = await classify_group_message(
        classifier,
        "В подъезде пахнет газом",
        min_confidence=0.72,
    )
    assert result is not None
    assert result.source == "urgent_fallback"
    assert result.severity == "urgent"


async def test_missing_deepseek_only_keeps_urgent_fallback() -> None:
    assert (
        await classify_group_message(
            None,
            "Лифт не работает второй час",
            min_confidence=0.72,
        )
        is None
    )
    urgent = await classify_group_message(
        None,
        "В щитке искрит",
        min_confidence=0.72,
    )
    assert urgent is not None
    assert urgent.source == "urgent_fallback"


async def test_fingerprint_uses_original_message_not_ai_summary() -> None:
    classifier = StubClassifier(
        result=DeepSeekResult(
            is_problem=True,
            problem="Не работает лифт.",
            confidence=0.95,
            severity="normal",
        )
    )
    first = await classify_group_message(
        classifier,
        "Лифт не работает!!!",
        min_confidence=0.72,
    )
    second = await classify_group_message(
        classifier,
        "  лифт   не работает ",
        min_confidence=0.72,
    )
    assert first is not None and second is not None
    assert first.fingerprint == second.fingerprint
