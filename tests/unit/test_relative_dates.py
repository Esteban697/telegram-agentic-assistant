"""Relative day expressions, resolved without a model or a network."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.policy.relative_dates import (
    UnknownDayExpression,
    parse_clock,
    resolve_day,
    resolve_day_range,
)

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)  # Monday
MADRID = ZoneInfo("Europe/Madrid")


def test_single_days() -> None:
    assert resolve_day("today", NOW, MADRID).isoformat() == "2026-10-19"
    assert resolve_day("tomorrow", NOW, MADRID).isoformat() == "2026-10-20"
    assert resolve_day("day_after_tomorrow", NOW, MADRID).isoformat() == "2026-10-21"
    assert resolve_day("yesterday", NOW, MADRID).isoformat() == "2026-10-18"


def test_weekdays_and_ranges() -> None:
    start, end = resolve_day_range("this_saturday", NOW, MADRID)
    assert start.isoformat() == "2026-10-24"
    assert end.isoformat() == "2026-10-25"

    start, end = resolve_day_range("next_saturday", NOW, MADRID)
    assert start.isoformat() == "2026-10-31"

    start, end = resolve_day_range("this_weekend", NOW, MADRID)
    assert start.isoformat() == "2026-10-24"
    assert end.isoformat() == "2026-10-26"

    start, end = resolve_day_range("this_week", NOW, MADRID)
    assert start.isoformat() == "2026-10-19"
    assert end.isoformat() == "2026-10-26"

    start, end = resolve_day_range("next_week", NOW, MADRID)
    assert start.isoformat() == "2026-10-26"
    assert end.isoformat() == "2026-11-02"


def test_expression_normalisation_is_accent_and_space_insensitive() -> None:
    assert resolve_day("  This-Saturday ", NOW, MADRID).isoformat() == "2026-10-24"


def test_unknown_expression_is_typed() -> None:
    with pytest.raises(UnknownDayExpression):
        resolve_day("whenever", NOW, MADRID)


def test_parse_clock() -> None:
    assert parse_clock("09:30").strftime("%H:%M") == "09:30"
    with pytest.raises(ValueError):
        parse_clock("9:30 am")
