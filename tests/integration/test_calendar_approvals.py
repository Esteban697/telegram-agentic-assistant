"""Calendar writes through the real approval machinery (fake provider).

The provider is a fake; the registry, the approval CAS, the ledger and the bot handler are the
real ones. This is the integration seam that proves a calendar change is prepared, not executed,
and that an ambiguous timeout becomes ``unknown``.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.errors import GoogleTimeout
from app.tools.calendar import CalendarTools
from app.tools.calendar import specs as calendar_specs
from app.tools.registry import ApprovalRequired
from tests.integration.test_phase3_approvals import (
    Phase3,
    answer,
    callback_update,
    message_update,
    tool_call,
)

pytestmark = pytest.mark.integration

CREATE_ARGS = {
    "summary": "Reunión con Ana",
    "start_local": "2026-11-03 10:00",
    "duration_minutes": 30,
}


class FakeCalendar:
    def __init__(self, *, create_error: Exception | None = None) -> None:
        self.create_error = create_error
        self.created: list[dict[str, Any]] = []
        self.send_updates: list[str] = []

    async def create_event(self, body: dict[str, Any], *, send_updates: str) -> dict[str, Any]:
        if self.create_error is not None:
            raise self.create_error
        self.created.append(body)
        self.send_updates.append(send_updates)
        return {
            "id": body["id"],
            "summary": body["summary"],
            "start": body["start"],
            "end": body["end"],
        }


def _calendar_tools(provider: FakeCalendar) -> list[Any]:
    return calendar_specs(
        CalendarTools(provider=provider, default_timezone="Europe/Madrid")
    )


async def test_a_calendar_write_is_prepared_and_only_runs_on_approval() -> None:
    provider = FakeCalendar()
    async with Phase3(
        script=[tool_call("create_calendar_event", CREATE_ARGS), answer()],
        extra_specs=_calendar_tools(provider),
    ) as p:
        await p.handlers.handle(message_update(1), p.identity())

        rows = await p.action_rows()
        assert len(rows) == 1, f"expected one prepared action, got {rows}"
        assert rows[0]["state"] == "pending"
        assert "Reunión con Ana" in rows[0]["preview_text"]
        assert provider.created == [], "the event was created before approval"

        data = await p.callback_for(rows[0]["action_id"])
        await p.handlers.handle(callback_update(2, data), p.identity())

        rows = await p.action_rows()
        assert rows[0]["state"] == "succeeded"
        assert len(provider.created) == 1
        assert provider.created[0]["id"].startswith("taa")
        assert provider.created[0]["summary"] == "Reunión con Ana"


async def test_a_repeated_calendar_callback_executes_once() -> None:
    provider = FakeCalendar()
    async with Phase3(
        script=[tool_call("create_calendar_event", CREATE_ARGS), answer()],
        extra_specs=_calendar_tools(provider),
    ) as p:
        await p.handlers.handle(message_update(1), p.identity())
        action_id = (await p.action_rows())[0]["action_id"]
        data = await p.callback_for(action_id)

        await p.handlers.handle(callback_update(2, data), p.identity())
        await p.handlers.handle(callback_update(3, data), p.identity())

        assert len(provider.created) == 1
        assert (await p.action_rows())[0]["state"] == "succeeded"


async def test_an_ambiguous_calendar_timeout_becomes_unknown() -> None:
    provider = FakeCalendar(create_error=GoogleTimeout("timed out"))
    async with Phase3(
        script=[tool_call("create_calendar_event", CREATE_ARGS), answer()],
        extra_specs=_calendar_tools(provider),
    ) as p:
        await p.handlers.handle(message_update(1), p.identity())
        action_id = (await p.action_rows())[0]["action_id"]
        data = await p.callback_for(action_id)

        await p.handlers.handle(callback_update(2, data), p.identity())

        rows = await p.action_rows()
        assert rows[0]["state"] == "unknown"
        assert provider.created == []


async def test_a_calendar_write_cannot_be_invoked_directly() -> None:
    provider = FakeCalendar()
    async with Phase3(
        script=[answer()],
        extra_specs=_calendar_tools(provider),
    ) as p:
        with pytest.raises(ApprovalRequired):
            await p.registry.invoke(
                tool_name="create_calendar_event",
                arguments=CREATE_ARGS,
                thread_id="thread-1",
                run_key="run-1",
                attempt_no=1,
                user_id=42,
            )
        assert provider.created == []
