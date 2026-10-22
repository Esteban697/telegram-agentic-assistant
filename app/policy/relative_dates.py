"""Relative day expressions, resolved in code (layer 0, pure).

The model is good at classifying "tomorrow" or "this Saturday" and bad at date arithmetic. These
helpers turn the classification into a concrete date using an explicit `now` and IANA zone, so the
calendar and reminder tools never depend on the model knowing the current date.

The expressions are English tokens (`today`, `tomorrow`, `this_saturday`, `next_week`, ...); the
Spanish product phrases are translated by the model in the prompt, not parsed here.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

WEEKDAY_INDEX: dict[str, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


class UnknownDayExpression(ValueError):
    """The expression is not one of the supported relative days."""

    def __init__(self, expression: str) -> None:
        super().__init__(f"unknown day expression: {expression!r}")
        self.expression = expression


def normalize_expression(text: str) -> str:
    cleaned = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[\s-]+", "_", cleaned.strip()).strip("_")


def resolve_day_range(expression: str, now: datetime, zone: ZoneInfo) -> tuple[date, date]:
    """Return the inclusive start date and exclusive end date for an expression."""

    key = normalize_expression(expression)
    today = now.astimezone(zone).date()

    if key == "today":
        return today, today + timedelta(days=1)
    if key == "tomorrow":
        return today + timedelta(days=1), today + timedelta(days=2)
    if key == "day_after_tomorrow":
        return today + timedelta(days=2), today + timedelta(days=3)
    if key == "yesterday":
        return today - timedelta(days=1), today

    if key in {"this_week", "week"}:
        return today, today + timedelta(days=7 - today.weekday())
    if key == "next_week":
        start = today + timedelta(days=7 - today.weekday())
        return start, start + timedelta(days=7)
    if key in {"this_weekend", "weekend"}:
        saturday = today + timedelta(days=(5 - today.weekday()) % 7)
        return saturday, saturday + timedelta(days=2)
    if key == "next_weekend":
        saturday = today + timedelta(days=(5 - today.weekday()) % 7 + 7)
        return saturday, saturday + timedelta(days=2)

    prefix_next = key.startswith("next_")
    base = key
    for prefix in ("next_", "this_"):
        if base.startswith(prefix):
            base = base[len(prefix) :]
            break
    if base in WEEKDAY_INDEX:
        start = today + timedelta(days=(WEEKDAY_INDEX[base] - today.weekday()) % 7)
        if prefix_next:
            start += timedelta(days=7)
        return start, start + timedelta(days=1)

    raise UnknownDayExpression(expression)


def resolve_day(expression: str, now: datetime, zone: ZoneInfo) -> date:
    """The first day of the expression's range; used by single-day tools such as reminders."""

    return resolve_day_range(expression, now, zone)[0]


def parse_clock(text: str) -> time:
    """Parse `HH:MM`; raises ValueError for anything else."""

    try:
        return datetime.strptime(text.strip(), "%H:%M").time()
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("time must be HH:MM") from exc


__all__ = [
    "UnknownDayExpression",
    "WEEKDAY_INDEX",
    "normalize_expression",
    "parse_clock",
    "resolve_day",
    "resolve_day_range",
]
