"""Reminder date resolution: relative days and the past-date refusal."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.tools.tasks import CreateReminderArgs, TaskTools, resolve_reminder_due

NOW = datetime(2026, 10, 23, 10, 0, tzinfo=UTC)  # 12:00 in Madrid


def test_relative_reminder_resolves_on_the_server() -> None:
    args = CreateReminderArgs(title="Llamar", day_expression="tomorrow", time="09:00")
    resolved = resolve_reminder_due(args, default_timezone="Europe/Madrid", now=NOW)

    # 24 October 2026 is still CEST (UTC+2).
    assert resolved.due_at_utc.isoformat() == "2026-10-24T07:00:00+00:00"
    assert resolved.timezone == "Europe/Madrid"


def test_explicit_due_local_still_works() -> None:
    args = CreateReminderArgs(title="Llamar", due_local="2026-10-24 09:00")
    resolved = resolve_reminder_due(args, default_timezone="Europe/Madrid", now=NOW)

    assert resolved.due_at_utc.isoformat() == "2026-10-24T07:00:00+00:00"


def test_a_past_reminder_is_refused_before_touching_the_database() -> None:
    tools = TaskTools(sessions=None, now=lambda: NOW)  # type: ignore[arg-type]
    result = asyncio.run(
        tools.create_reminder(
            user_id=1,
            args=CreateReminderArgs(title="Ayer", day_expression="yesterday", time="09:00"),
        )
    )

    assert result.ok is False
    assert result.error_code == "reminder_in_past"
    assert result.message_key == "error.reminder_in_past"


def test_day_expression_requires_a_time_and_wins_over_due_local() -> None:
    with pytest.raises(ValidationError):
        CreateReminderArgs(title="A", day_expression="tomorrow")

    args = CreateReminderArgs(
        title="A",
        due_local="2026-10-25 09:00",
        day_expression="tomorrow",
        time="09:00",
    )
    resolved = resolve_reminder_due(args, default_timezone="Europe/Madrid", now=NOW)
    assert resolved.due_at_utc.isoformat() == "2026-10-24T07:00:00+00:00"
