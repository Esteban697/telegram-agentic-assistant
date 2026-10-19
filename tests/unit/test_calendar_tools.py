"""Calendar tools: risk levels, body shapes, idempotency and failure semantics."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from app.errors import (
    CalendarEventChanged,
    GoogleBadRequest,
    GoogleConflict,
    GoogleTimeout,
)
from app.agent.policies import preview_text_for
from app.policy.risk import RiskLevel
from app.tools.calendar import (
    CalendarTools,
    CancelEventArgs,
    CreateEventArgs,
    FreeSlotsArgs,
    ListEventsArgs,
    UpdateEventArgs,
    specs,
)
from app.tools.clock import ClockTool


class FakeProvider:
    def __init__(
        self,
        *,
        events: list[dict[str, Any]] | None = None,
        busy: list[dict[str, Any]] | None = None,
        get_event: dict[str, Any] | None = None,
        create_error: Exception | None = None,
        get_error: Exception | None = None,
        update_error: Exception | None = None,
        delete_error: Exception | None = None,
    ) -> None:
        self.events = events or []
        self.busy = busy or []
        self.event = get_event or {}
        self.create_error = create_error
        self.get_error = get_error
        self.update_error = update_error
        self.delete_error = delete_error
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.deleted: list[dict[str, Any]] = []
        self.sent: list[str] = []

    async def list_events(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return self.events

    async def get_event(self, _event_id: str) -> dict[str, Any]:
        if self.get_error is not None:
            raise self.get_error
        return self.event

    async def create_event(self, body: dict[str, Any], *, send_updates: str) -> dict[str, Any]:
        if self.create_error is not None:
            raise self.create_error
        self.created.append(body)
        self.sent.append(send_updates)
        return {"id": body["id"], "summary": body["summary"], "start": body["start"], "end": body["end"]}

    async def update_event(
        self, event_id: str, body: dict[str, Any], *, send_updates: str
    ) -> dict[str, Any]:
        if self.update_error is not None:
            raise self.update_error
        self.updated.append(body)
        self.sent.append(send_updates)
        return {"id": event_id, **body}

    async def delete_event(self, event_id: str, *, send_updates: str) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append({"event_id": event_id})
        self.sent.append(send_updates)

    async def free_busy(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return self.busy


def test_tool_levels_and_names() -> None:
    levels = {spec.name: spec.risk_level for spec in specs(CalendarTools(provider=None))}

    assert levels == {
        "list_calendar_events": RiskLevel.READ,
        "get_calendar_event": RiskLevel.READ,
        "find_free_slots": RiskLevel.READ,
        "create_calendar_event": RiskLevel.EXTERNAL,
        "update_calendar_event": RiskLevel.EXTERNAL,
        "cancel_calendar_event": RiskLevel.EXTERNAL,
    }


async def test_create_event_builds_the_body_and_deterministic_id() -> None:
    provider = FakeProvider()
    tools = CalendarTools(provider=provider, default_timezone="Europe/Madrid")

    result = await tools.create_event(
        user_id=1,
        args=CreateEventArgs(
            summary="Reunión con Ana",
            start_local="2026-01-15 10:00",
            duration_minutes=45,
            attendees=["Ana@Example.com", "ana@example.com"],
            add_meet=True,
        ),
        context={"action_id": "abc-123"},
    )

    assert result.ok is True
    body = provider.created[0]
    assert body["id"] == "taaabc123"
    assert body["start"]["dateTime"] == "2026-01-15T09:00:00Z"
    assert body["end"]["dateTime"] == "2026-01-15T09:45:00Z"
    assert body["attendees"] == [{"email": "ana@example.com"}]
    assert body["conferenceData"]["createRequest"]["requestId"].startswith("taa-")
    assert provider.sent == ["all"]


async def test_create_event_without_action_id_uses_a_content_hash() -> None:
    provider = FakeProvider()
    tools = CalendarTools(provider=provider, default_timezone="Europe/Madrid")

    first = await tools.create_event(
        user_id=1,
        args=CreateEventArgs(summary="A", start_local="2026-01-15 10:00"),
        context={},
    )
    second = await tools.create_event(
        user_id=1,
        args=CreateEventArgs(summary="A", start_local="2026-01-15 10:00"),
        context={},
    )

    assert first.ok and second.ok
    assert provider.created[0]["id"] == provider.created[1]["id"]


async def test_create_event_rejects_an_invalid_local_time() -> None:
    provider = FakeProvider()
    tools = CalendarTools(provider=provider, default_timezone="Europe/Madrid")

    result = await tools.create_event(
        user_id=1,
        args=CreateEventArgs(summary="DST", start_local="2026-03-29 02:30"),
        context={"action_id": "x"},
    )

    assert result.ok is False
    assert result.error_code == "invalid_local_time"
    assert provider.created == []


async def test_create_event_raises_on_an_ambiguous_timeout() -> None:
    provider = FakeProvider(create_error=GoogleTimeout("timed out"))
    tools = CalendarTools(provider=provider, default_timezone="Europe/Madrid")

    with pytest.raises(GoogleTimeout):
        await tools.create_event(
            user_id=1,
            args=CreateEventArgs(summary="A", start_local="2026-01-15 10:00"),
            context={"action_id": "x"},
        )


async def test_create_event_returns_a_definite_failure() -> None:
    provider = FakeProvider(create_error=GoogleBadRequest("bad"))
    tools = CalendarTools(provider=provider, default_timezone="Europe/Madrid")

    result = await tools.create_event(
        user_id=1,
        args=CreateEventArgs(summary="A", start_local="2026-01-15 10:00"),
        context={"action_id": "x"},
    )

    assert result.ok is False
    assert result.error_code == "google_bad_request"


async def test_create_event_reconciles_a_conflict_by_reading_the_event() -> None:
    provider = FakeProvider(
        create_error=GoogleConflict("exists"),
        get_event={
            "id": "taax",
            "summary": "A",
            "start": {"dateTime": "2026-01-15T09:00:00Z"},
            "end": {"dateTime": "2026-01-15T10:00:00Z"},
        },
    )
    tools = CalendarTools(provider=provider, default_timezone="Europe/Madrid")

    result = await tools.create_event(
        user_id=1,
        args=CreateEventArgs(summary="A", start_local="2026-01-15 10:00"),
        context={"action_id": "x"},
    )

    assert result.ok is True
    assert result.meta.get("reconciled") is True


async def test_update_refuses_an_event_that_no_longer_matches() -> None:
    provider = FakeProvider(
        get_event={
            "id": "e1",
            "summary": "Otra cosa",
            "start": {"dateTime": "2026-01-15T09:00:00Z"},
        }
    )
    tools = CalendarTools(provider=provider, default_timezone="Europe/Madrid")

    result = await tools.update_event(
        user_id=1,
        args=UpdateEventArgs(
            event_id="e1",
            expected_summary="Reunión",
            expected_start="2026-01-15T09:00:00Z",
            summary="Nueva",
        ),
        context={"action_id": "x"},
    )

    assert result.ok is False
    assert result.error_code == "calendar_event_changed"
    assert provider.updated == []


async def test_cancel_notifies_attendees() -> None:
    provider = FakeProvider(
        get_event={
            "id": "e1",
            "summary": "Reunión",
            "start": {"dateTime": "2026-01-15T09:00:00Z"},
            "attendees": [{"email": "ana@example.com"}],
        }
    )
    tools = CalendarTools(provider=provider, default_timezone="Europe/Madrid")

    result = await tools.cancel_event(
        user_id=1,
        args=CancelEventArgs(
            event_id="e1",
            expected_summary="Reunión",
            expected_start="2026-01-15T09:00:00Z",
        ),
    )

    assert result.ok is True
    assert provider.deleted == [{"event_id": "e1"}]
    assert provider.sent == ["all"]


async def test_list_events_rejects_a_range_over_the_cap() -> None:
    provider = FakeProvider()
    tools = CalendarTools(provider=provider, max_range_days=30)

    result = await tools.list_events(
        user_id=1,
        args=ListEventsArgs(start_local="2026-01-01 00:00", end_local="2026-12-31 23:00"),
    )

    assert result.ok is False
    assert result.error_code == "calendar_limit"


async def test_find_free_slots_excludes_busy_time() -> None:
    provider = FakeProvider(
        busy=[{"start": "2026-01-01T10:00:00Z", "end": "2026-01-01T11:00:00Z"}]
    )
    tools = CalendarTools(provider=provider, default_timezone="Europe/Madrid")

    result = await tools.find_free_slots(
        user_id=1,
        args=FreeSlotsArgs(
            start_local="2026-01-01 09:00",
            end_local="2026-01-01 12:00",
            duration_minutes=60,
        ),
    )

    assert result.ok is True
    slots = result.data["slots"]
    assert len(slots) == 1
    assert slots[0]["start_local"] == "01/01/2026 09:00"
    assert slots[0]["end_local"] == "01/01/2026 11:00"


async def test_clock_reports_the_local_date_and_weekday() -> None:
    tool = ClockTool(
        timezone="Europe/Madrid",
        now=lambda: datetime(2026, 10, 19, 12, 0, tzinfo=UTC),
    )

    result = await tool.get_current_datetime(user_id=1)

    assert result.ok is True
    assert result.data["date"] == "2026-10-19"
    assert result.data["time"] == "14:00"
    assert result.data["weekday"] == "Monday"
    assert result.data["weekday_index"] == 0


def test_attendee_emails_are_validated_and_deduplicated() -> None:
    args = CreateEventArgs(
        summary="A",
        start_local="2026-01-15 10:00",
        attendees=["Ana@Example.com", "ana@example.com"],
    )
    assert args.attendees == ["ana@example.com"]

    with pytest.raises(ValidationError):
        CreateEventArgs(
            summary="A",
            start_local="2026-01-15 10:00",
            attendees=["not-an-email"],
        )


async def test_missing_provider_is_capability_unavailable() -> None:
    result = await CalendarTools(provider=None).list_events(
        user_id=1,
        args=ListEventsArgs(start_local="2026-01-01 00:00", end_local="2026-01-02 00:00"),
    )

    assert result.ok is False
    assert result.error_code == "CapabilityUnavailable"


def test_the_approval_preview_is_capped_below_telegrams_limit() -> None:
    text = preview_text_for("create_calendar_event", {"description": "x" * 5000})

    assert len(text) <= 3000
    assert text.endswith("…")
