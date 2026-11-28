"""The 07:30 morning brief: today's agenda, reminders, weather and yesterday's spend.

A one-shot process like every other job in this package: Cloud Scheduler runs it, it gathers
four **independent** sections, renders them through the catalogue and exits. A section that
fails degrades only itself — a slow weather API must not cost the user the agenda, and an
expired Google authorization must not hide the reminders.

    MORNING_BRIEF_ENABLED=true python -m app.jobs.morning_brief
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.bot import messages
from app.bot.morning_brief import (
    CALENDAR_AUTH,
    CALENDAR_DISABLED,
    CALENDAR_OK,
    CALENDAR_UNAVAILABLE,
    AgendaEntry,
    ReminderEntry,
    compose_brief,
)
from app.bot.telegram_client import TelegramClient
from app.config import Settings, load_settings
from app.errors import GoogleAuthError, GoogleError
from app.integrations.google_calendar import build_calendar_provider
from app.integrations.weather import WeatherUnavailable, WeatherSnapshot, fetch_weather
from app.jobs import report_line
from app.logging_config import configure_logging
from app.services.billing import BillingService
from app.storage import repositories
from app.storage.postgres import build_database, use_compatible_event_loop

_logger = logging.getLogger(__name__)

# A day with more than this many events is not a brief any more, and 50 events plus the other
# sections would approach Telegram's 4096-character ceiling.
_AGENDA_LIMIT = 20
_SUMMARY_MAX_CHARS = 120
_LOCATION_MAX_CHARS = 60


def _parse_start(raw: Any, zone: ZoneInfo) -> datetime | None:
    """Local start time, or ``None`` for an all-day event (Google sends a date, not a time)."""

    if not isinstance(raw, dict):
        return None
    moment = raw.get("dateTime")
    if not isinstance(moment, str) or not moment:
        return None
    try:
        parsed = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def _agenda_entries(events: list[dict[str, Any]], zone: ZoneInfo) -> list[AgendaEntry]:
    entries: list[AgendaEntry] = []
    for item in events:
        if str(item.get("status") or "") == "cancelled":
            continue
        summary = str(item.get("summary") or messages.render("brief.agenda.untitled")).strip()
        location = item.get("location")
        entries.append(
            AgendaEntry(
                summary=summary[:_SUMMARY_MAX_CHARS],
                start=_parse_start(item.get("start"), zone),
                location=(
                    str(location).strip()[:_LOCATION_MAX_CHARS] if location else None
                ),
            )
        )
    # Ordering is the composer's job (all-day first, then chronological): this function only
    # filters and normalises what the provider returned.
    return entries


async def _weather(settings: Settings, zone: ZoneInfo) -> WeatherSnapshot | None:
    try:
        return await fetch_weather(
            latitude=settings.morning_brief_latitude,
            longitude=settings.morning_brief_longitude,
            timezone=settings.default_timezone,
            timeout=settings.morning_brief_timeout_seconds,
        )
    except WeatherUnavailable:
        _logger.info("morning brief: weather unavailable", extra={"status": "weather_missing"})
        return None


async def _agenda(
    settings: Settings, *, day_start: datetime, day_end: datetime, zone: ZoneInfo
) -> tuple[list[AgendaEntry], str]:
    provider = build_calendar_provider(settings)
    if provider is None:
        return [], CALENDAR_DISABLED
    try:
        events = await provider.list_events(
            time_min=day_start, time_max=day_end, max_results=_AGENDA_LIMIT
        )
        return _agenda_entries(events, zone), CALENDAR_OK
    except GoogleAuthError:
        # The refresh token expired (Testing-mode consent screens expire them weekly). Say so in
        # the brief instead of silently presenting a day with no meetings.
        _logger.warning("morning brief: calendar authorization expired")
        return [], CALENDAR_AUTH
    except GoogleError:
        _logger.warning("morning brief: calendar unavailable")
        return [], CALENDAR_UNAVAILABLE
    finally:
        await provider.aclose()


async def _reminders(
    settings: Settings, *, user_id: int, day_start: datetime, day_end: datetime, zone: ZoneInfo
) -> list[ReminderEntry] | None:
    database = build_database(settings)
    try:
        async with database.sessions() as session:
            tasks = await repositories.list_tasks_due_between(
                session, user_id=user_id, start=day_start, end=day_end
            )
        return [
            ReminderEntry(title=task.title, due_at=task.due_at.astimezone(zone))
            for task in tasks
        ]
    except Exception:  # noqa: BLE001 - the brief survives a database blip; the report says so
        _logger.exception("morning brief: reminders unavailable")
        return None
    finally:
        await database.dispose()


async def _yesterday_spend(
    settings: Settings, *, yesterday: str, service: Any = None
) -> tuple[float, str] | None:
    billing = service if service is not None else BillingService(settings)
    try:
        snapshot = await billing.fetch_rows(days=3)
    except Exception:  # noqa: BLE001 - billing is optional; the export may not exist yet
        _logger.info("morning brief: billing unavailable", extra={"status": "billing_missing"})
        return None
    if snapshot is None:
        return None
    rows = [row for row in snapshot.rows if str(row.get("day")) == yesterday]
    assistant_rows = [row for row in rows if str(row.get("project_id")) == billing.project]
    if not assistant_rows:
        return None
    currency = str(assistant_rows[0].get("currency") or "EUR")
    total = 0.0
    for row in assistant_rows:
        try:
            total += float(row.get("net") or 0)
        except (TypeError, ValueError):
            continue
    return total, currency


async def run() -> dict[str, Any]:
    settings = load_settings().validate_or_raise()
    configure_logging(
        level=settings.log_level,
        app_env=settings.app_env,
        content_debug=settings.log_content_debug,
    )
    if not settings.morning_brief_enabled:
        return {"status": "disabled"}

    user_id = next(iter(settings.telegram_allowed_user_ids), None)
    if user_id is None:
        return {"status": "no_chat_id"}

    zone = ZoneInfo(settings.default_timezone)
    now = datetime.now(zone)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    yesterday = (day_start - timedelta(days=1)).date().isoformat()

    weather = await _weather(settings, zone)
    agenda, calendar_state = await _agenda(
        settings, day_start=day_start, day_end=day_end, zone=zone
    )
    reminders = await _reminders(
        settings, user_id=user_id, day_start=day_start, day_end=day_end, zone=zone
    )
    spend = await _yesterday_spend(settings, yesterday=yesterday)

    text = compose_brief(
        now=now,
        city=settings.morning_brief_city,
        agenda=agenda,
        calendar_state=calendar_state,
        reminders=reminders or [],
        weather=weather,
        spend=spend,
    )

    telegram = TelegramClient(token=settings.bot_token, base_url=settings.telegram_api_base_url)
    try:
        await telegram.send_message(chat_id=user_id, text=text)
    finally:
        await telegram.aclose()

    return {
        "status": "sent",
        "agenda": len(agenda),
        "calendar": calendar_state,
        "reminders": None if reminders is None else len(reminders),
        "weather": weather is not None,
        "spend": spend is not None,
    }


def main() -> int:
    use_compatible_event_loop()
    try:
        report = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - a job reports, it does not crash silently
        _logger.exception("morning brief failed", extra={"status": "failed"})
        print(report_line({"status": "failed", "error": type(exc).__name__}))
        return 1
    print(report_line(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
