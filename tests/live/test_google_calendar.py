"""Live Google Calendar check against a dedicated test calendar.

Skipped unless both the OAuth credentials and ``GOOGLE_CALENDAR_TEST_ID`` are available. It
never touches ``primary``: the test creates, updates and deletes one uniquely named event in the
test calendar, then verifies it is gone.

Run with:

    GOOGLE_CALENDAR_TEST_ID=<secondary-calendar-id> RUN_LIVE_TESTS=1 \
      uv run pytest -m "live and not eval" tests/live/test_google_calendar.py
"""

from __future__ import annotations

import json
import os
import pathlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.errors import CalendarEventNotFound
from app.integrations.google_calendar import (
    GoogleCalendarProvider,
    OAuthTokenCache,
    parse_credentials,
)

pytestmark = pytest.mark.live

DEFAULT_CREDENTIALS = pathlib.Path.home() / ".taa" / "secrets" / "google-oauth-credentials"


def _raw_credentials() -> str:
    if os.environ.get("GOOGLE_OAUTH_CREDENTIALS"):
        return os.environ["GOOGLE_OAUTH_CREDENTIALS"]
    path = pathlib.Path(os.environ.get("GOOGLE_OAUTH_CREDENTIALS_FILE", DEFAULT_CREDENTIALS))
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def _test_calendar() -> str:
    return os.environ.get("GOOGLE_CALENDAR_TEST_ID", "")


credentials = _raw_credentials()
test_calendar = _test_calendar()

requires_calendar = pytest.mark.skipif(
    not credentials or not test_calendar or test_calendar == "primary",
    reason="set GOOGLE_CALENDAR_TEST_ID (not primary) and GOOGLE_OAUTH_CREDENTIALS to run this",
)


@requires_calendar
async def test_create_update_delete_and_freebusy_against_the_test_calendar() -> None:
    provider = GoogleCalendarProvider(
        credentials=parse_credentials(credentials),
        calendar_id=test_calendar,
        timeout=20.0,
        max_results=20,
    )
    now = datetime.now(UTC).replace(microsecond=0)
    start = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0)
    end = start + timedelta(minutes=30)
    event_id = f"taa{uuid.uuid4().hex}"
    created = False
    try:
        busy = await provider.free_busy(
            time_min=start - timedelta(days=1),
            time_max=end + timedelta(days=1),
            time_zone="UTC",
        )
        assert isinstance(busy, list)

        event = await provider.create_event(
            {
                "id": event_id,
                "summary": "TAA integration test",
                "start": {"dateTime": start.isoformat().replace("+00:00", "Z")},
                "end": {"dateTime": end.isoformat().replace("+00:00", "Z")},
            },
            send_updates="none",
        )
        created = True
        assert event["id"] == event_id

        fetched = await provider.get_event(event_id)
        assert fetched["summary"] == "TAA integration test"

        body = {
            "id": event_id,
            "summary": "TAA integration test (updated)",
            "start": {"dateTime": start.isoformat().replace("+00:00", "Z")},
            "end": {"dateTime": end.isoformat().replace("+00:00", "Z")},
        }
        updated = await provider.update_event(event_id, body, send_updates="none")
        assert updated["summary"].endswith("(updated)")

        await provider.delete_event(event_id, send_updates="none")
        created = False
        with pytest.raises(CalendarEventNotFound):
            await provider.get_event(event_id)
    finally:
        if created:
            try:
                await provider.delete_event(event_id, send_updates="none")
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
        await provider.aclose()
