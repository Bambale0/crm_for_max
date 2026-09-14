"""Pure group-chat classification rules without MAX or database dependencies."""

import pytest

from app.bot.chat_signals import classify_group_problem


@pytest.mark.parametrize(
    "text",
    [
        "Кто сегодня смотрел футбол?",
        "На улице очень холодно сегодня",
        "Лифт уже работает, починили",
        "Вода есть, всё нормально",
        "Ты вообще мусор какой-то",
        "Капает дождь весь день",
        "Обсуждаем отопление на собрании завтра",
    ],
)
def test_group_classifier_ignores_chatter_and_resolved_messages(text: str) -> None:
    assert classify_group_problem(text) is None


@pytest.mark.parametrize(
    ("text", "severity"),
    [
        ("Лифт не работает второй час", "normal"),
        ("Нет воды во всём подъезде", "normal"),
        ("Горячей воды нет с утра", "normal"),
        ("Света нет на лестнице", "normal"),
        ("Из трубы под раковиной течёт вода", "normal"),
        ("Холодные батареи, отопления нет", "normal"),
        ("Мусор не вывезли, контейнер переполнен", "normal"),
        ("Мусор не вывозят третий день", "normal"),
        ("Лифт застрял между этажами", "normal"),
        ("Вонь из канализации в подъезде", "normal"),
        ("В подъезде пахнет газом", "urgent"),
        ("Прорвало стояк, затапливает этаж", "urgent"),
        ("В щитке искрит", "urgent"),
    ],
)
def test_group_classifier_keeps_actionable_problems(text: str, severity: str) -> None:
    result = classify_group_problem(text)
    assert result is not None
    assert result.problem == text
    assert result.severity == severity


def test_group_classifier_normalizes_duplicate_fingerprint() -> None:
    first = classify_group_problem("Лифт не работает!!!")
    second = classify_group_problem("  лифт   не работает ")
    assert first is not None and second is not None
    assert first.fingerprint == second.fingerprint


def test_group_classifier_keeps_problem_after_resolved_clause() -> None:
    result = classify_group_problem("Вода есть, но труба под раковиной течёт")
    assert result is not None
    assert result.severity == "normal"
