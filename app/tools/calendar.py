"""Google Calendar tools (spec §10, PLAN.md §13.3).

Layer 4. Reads are level 0 and free; every external write is level 2, so it is prepared by the
graph and only runs through ``ToolRegistry.execute_approved`` with the user's approval.

Time policy: the model supplies a local ``YYYY-MM-DD HH:MM`` plus an IANA zone, resolved with
``app/policy/timezones.parse_local`` so DST gaps/overlaps are rejected instead of shifted.

Failure policy: definite failures (bad args, auth, 404, changed event, quota) become
``ToolResult.failure``; ambiguous outcomes (timeout, transport, 5xx) are raised so the approval
handler records the action as ``unknown`` and never retries it automatically.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator

from app.errors import (
    CalendarEventChanged,
    CalendarEventNotFound,
    GoogleAuthError,
    GoogleBadRequest,
    GoogleBadResponse,
    GoogleConflict,
    GoogleError,
    GoogleRateLimited,
    GoogleTimeout,
    GoogleUnavailable,
)
from app.policy.risk import RiskLevel
from app.policy.timezones import InvalidLocalTime, describe_instant, load_zone, parse_local
from app.tools.base import ToolResult
from app.tools.registry import ToolSpec

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MAX_DESCRIPTION_CHARS = 4000
_MAX_LOCATION_CHARS = 500
_MAX_RETURNED_DESCRIPTION = 1200
_MAX_SLOTS = 20
_MAX_EVENT_ID_CHARS = 1024

# Definite: the write did not happen (or the event is already gone), so the approval is FAILED.
_DEFINITE = (
    GoogleAuthError,
    GoogleBadRequest,
    GoogleRateLimited,
    CalendarEventNotFound,
    CalendarEventChanged,
    GoogleConflict,
)
# Ambiguous: the write may have happened; re-raise so the handler records UNKNOWN.
_AMBIGUOUS = (GoogleTimeout, GoogleUnavailable, GoogleBadResponse)


class ListEventsArgs(BaseModel):
    start_local: str = Field(min_length=8, max_length=32)
    end_local: str = Field(min_length=8, max_length=32)
    timezone: str | None = Field(default=None, max_length=64)
    query: str | None = Field(default=None, max_length=200)
    max_results: int | None = Field(default=None, ge=1, le=250)


class GetEventArgs(BaseModel):
    event_id: str = Field(min_length=1, max_length=_MAX_EVENT_ID_CHARS)


class FreeSlotsArgs(BaseModel):
    start_local: str = Field(min_length=8, max_length=32)
    end_local: str = Field(min_length=8, max_length=32)
    timezone: str | None = Field(default=None, max_length=64)
    duration_minutes: int = Field(default=60, ge=1, le=1440)
    include_weekends: bool = False


def _clean_attendees(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    cleaned: list[str] = []
    for raw in value:
        email = raw.strip().lower()
        if not _EMAIL_RE.match(email):
            raise ValueError(f"not an email address: {raw!r}")
        if email not in cleaned:
            cleaned.append(email)
    return cleaned


class CreateEventArgs(BaseModel):
    summary: str = Field(min_length=1, max_length=200)
    start_local: str = Field(min_length=8, max_length=32)
    end_local: str | None = Field(default=None, min_length=8, max_length=32)
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    timezone: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=_MAX_DESCRIPTION_CHARS)
    location: str | None = Field(default=None, max_length=_MAX_LOCATION_CHARS)
    attendees: list[str] = Field(default_factory=list, max_length=20)
    add_meet: bool = False

    @field_validator("attendees")
    @classmethod
    def _attendee_emails(cls, value: list[str]) -> list[str]:
        return _clean_attendees(value) or []

    @model_validator(mode="after")
    def _one_end(self) -> CreateEventArgs:
        if self.end_local is not None and self.duration_minutes is not None:
            raise ValueError("give end_local or duration_minutes, not both")
        return self


class UpdateEventArgs(BaseModel):
    event_id: str = Field(min_length=1, max_length=_MAX_EVENT_ID_CHARS)
    expected_summary: str = Field(min_length=1, max_length=200)
    expected_start: str = Field(min_length=1, max_length=64)
    summary: str | None = Field(default=None, min_length=1, max_length=200)
    start_local: str | None = Field(default=None, min_length=8, max_length=32)
    end_local: str | None = Field(default=None, min_length=8, max_length=32)
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    timezone: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=_MAX_DESCRIPTION_CHARS)
    location: str | None = Field(default=None, max_length=_MAX_LOCATION_CHARS)
    attendees: list[str] | None = Field(default=None, max_length=20)
    add_meet: bool | None = None

    @field_validator("attendees")
    @classmethod
    def _attendee_emails(cls, value: list[str] | None) -> list[str] | None:
        return _clean_attendees(value)

    @model_validator(mode="after")
    def _at_least_one_change(self) -> UpdateEventArgs:
        changes = (
            self.summary,
            self.start_local,
            self.end_local,
            self.duration_minutes,
            self.description,
            self.location,
            self.attendees,
            self.add_meet,
        )
        if all(item is None for item in changes):
            raise ValueError("the update has no fields to change")
        if self.end_local is not None and self.duration_minutes is not None:
            raise ValueError("give end_local or duration_minutes, not both")
        return self


class CancelEventArgs(BaseModel):
    event_id: str = Field(min_length=1, max_length=_MAX_EVENT_ID_CHARS)
    expected_summary: str = Field(min_length=1, max_length=200)
    expected_start: str = Field(min_length=1, max_length=64)


@dataclass
class CalendarTools:
    """Calendar operations over an injected provider (``None`` in credential-free tests)."""

    provider: Any | None
    calendar_id: str = "primary"
    default_timezone: str = "Europe/Madrid"
    max_results: int = 50
    max_range_days: int = 90
    max_attendees: int = 20
    workday_start_hour: int = 9
    workday_end_hour: int = 18
    default_duration_minutes: int = 60

    # ------------------------------------------------------------------ #
    # Reads (level 0)
    # ------------------------------------------------------------------ #
    async def list_events(
        self, *, user_id: int, args: ListEventsArgs, **_: Any
    ) -> ToolResult[list[dict[str, Any]]]:
        if self.provider is None:
            return _missing_provider()
        try:
            start, end, zone = self._range(args.start_local, args.end_local, args.timezone)
        except InvalidLocalTime as exc:
            return _invalid_time(exc)
        if end <= start:
            return ToolResult.failure(
                "invalid_range",
                message_key="error.calendar_bad_request",
                detail="the range ends before it starts",
            )
        if (end - start).days > self.max_range_days:
            return _limit()
        try:
            events = await self.provider.list_events(
                time_min=start,
                time_max=end,
                query=args.query,
                max_results=min(args.max_results or self.max_results, self.max_results),
            )
        except GoogleError as exc:
            return _read_failure(exc)
        return ToolResult.success([_normalize_event(item) for item in events])

    async def get_event(
        self, *, user_id: int, args: GetEventArgs, **_: Any
    ) -> ToolResult[dict[str, Any]]:
        if self.provider is None:
            return _missing_provider()
        try:
            event = await self.provider.get_event(args.event_id)
        except GoogleError as exc:
            return _read_failure(exc)
        return ToolResult.success(_normalize_event(event))

    async def find_free_slots(
        self, *, user_id: int, args: FreeSlotsArgs, **_: Any
    ) -> ToolResult[dict[str, Any]]:
        if self.provider is None:
            return _missing_provider()
        try:
            start, end, zone = self._range(args.start_local, args.end_local, args.timezone)
        except InvalidLocalTime as exc:
            return _invalid_time(exc)
        if end <= start:
            return ToolResult.failure(
                "invalid_range",
                message_key="error.calendar_bad_request",
                detail="the range ends before it starts",
            )
        if (end - start).days > self.max_range_days:
            return _limit()
        try:
            busy_raw = await self.provider.free_busy(
                time_min=start, time_max=end, time_zone=zone.key
            )
        except GoogleError as exc:
            return _read_failure(exc)
        busy = _parse_busy(busy_raw)
        slots = _free_slots(
            start=start,
            end=end,
            busy=busy,
            zone=zone,
            duration=timedelta(minutes=args.duration_minutes),
            work_start_hour=self.workday_start_hour,
            work_end_hour=self.workday_end_hour,
            include_weekends=args.include_weekends,
        )
        return ToolResult.success(
            {
                "timezone": zone.key,
                "duration_minutes": args.duration_minutes,
                "slots": slots[:_MAX_SLOTS],
                "truncated": len(slots) > _MAX_SLOTS,
            }
        )

    # ------------------------------------------------------------------ #
    # Writes (level 2, approved)
    # ------------------------------------------------------------------ #
    async def create_event(
        self, *, user_id: int, args: CreateEventArgs, context: dict[str, Any] | None = None, **_: Any
    ) -> ToolResult[dict[str, Any]]:
        if self.provider is None:
            return _missing_provider()
        zone = args.timezone or self.default_timezone
        try:
            start_instant = parse_local(args.start_local, zone).due_at_utc
            end_instant = self._end_for_create(args, start_instant, zone)
        except InvalidLocalTime as exc:
            return _invalid_time(exc)
        if end_instant <= start_instant:
            return ToolResult.failure(
                "invalid_range",
                message_key="error.calendar_bad_request",
                detail="the event ends before it starts",
            )
        if len(args.attendees) > self.max_attendees:
            return _limit()
        event_id = _event_id(args, context)
        body: dict[str, Any] = {
            "id": event_id,
            "summary": args.summary,
            "start": {
                "dateTime": _rfc3339(start_instant),
                "timeZone": zone,
            },
            "end": {
                "dateTime": _rfc3339(end_instant),
                "timeZone": zone,
            },
        }
        if args.description:
            body["description"] = args.description
        if args.location:
            body["location"] = args.location
        if args.attendees:
            body["attendees"] = [{"email": email} for email in args.attendees]
        if args.add_meet:
            body["conferenceData"] = _meet_request(event_id)
        send_updates = "all" if args.attendees else "none"
        try:
            event = await self.provider.create_event(body, send_updates=send_updates)
        except GoogleConflict as exc:
            try:
                existing = await self.provider.get_event(event_id)
            except _DEFINITE:
                return _write_failure(exc)
            if _same_event(existing, summary=args.summary, start=body["start"]["dateTime"]):
                return ToolResult.success(
                    _normalize_event(existing), reconciled=True, event_id=event_id
                )
            return _write_failure(
                GoogleConflict(
                    "an event with the deterministic id exists with different fields",
                    context={"event_id": event_id},
                )
            )
        except _DEFINITE as exc:
            return _write_failure(exc)
        return ToolResult.success(
            _normalize_event(event),
            event_id=event_id,
            attendees_notified=bool(args.attendees),
        )

    async def update_event(
        self, *, user_id: int, args: UpdateEventArgs, context: dict[str, Any] | None = None, **_: Any
    ) -> ToolResult[dict[str, Any]]:
        if self.provider is None:
            return _missing_provider()
        zone = args.timezone or self.default_timezone
        try:
            current = await self.provider.get_event(args.event_id)
        except _DEFINITE as exc:
            return _write_failure(exc)
        if not _matches_expected(current, args.expected_summary, args.expected_start):
            return _write_failure(
                CalendarEventChanged(
                    "the event does not match the summary/start the user approved",
                    context={"event_id": args.event_id},
                )
            )
        body = _writable_event(current)
        if args.summary is not None:
            body["summary"] = args.summary
        if args.description is not None:
            body["description"] = args.description
        if args.location is not None:
            body["location"] = args.location
        if args.attendees is not None:
            if len(args.attendees) > self.max_attendees:
                return _limit()
            body["attendees"] = [{"email": email} for email in args.attendees]

        if args.start_local is not None or args.end_local is not None or args.duration_minutes:
            try:
                start_instant, end_instant = self._updated_times(current, args, zone)
            except InvalidLocalTime as exc:
                return _invalid_time(exc)
            if end_instant <= start_instant:
                return ToolResult.failure(
                    "invalid_range",
                    message_key="error.calendar_bad_request",
                    detail="the event ends before it starts",
                )
            body["start"] = {"dateTime": _rfc3339(start_instant), "timeZone": zone}
            body["end"] = {"dateTime": _rfc3339(end_instant), "timeZone": zone}

        if args.add_meet and not body.get("conferenceData"):
            action_id = str((context or {}).get("action_id") or args.event_id)
            body["conferenceData"] = _meet_request(action_id)

        has_attendees = bool(current.get("attendees") or body.get("attendees"))
        send_updates = "all" if has_attendees else "none"
        try:
            event = await self.provider.update_event(
                args.event_id, body, send_updates=send_updates
            )
        except _DEFINITE as exc:
            return _write_failure(exc)
        return ToolResult.success(
            _normalize_event(event),
            event_id=args.event_id,
            attendees_notified=has_attendees,
        )

    async def cancel_event(
        self, *, user_id: int, args: CancelEventArgs, **_: Any
    ) -> ToolResult[dict[str, Any]]:
        if self.provider is None:
            return _missing_provider()
        try:
            current = await self.provider.get_event(args.event_id)
        except _DEFINITE as exc:
            return _write_failure(exc)
        if not _matches_expected(current, args.expected_summary, args.expected_start):
            return _write_failure(
                CalendarEventChanged(
                    "the event does not match the summary/start the user approved",
                    context={"event_id": args.event_id},
                )
            )
        has_attendees = bool(current.get("attendees"))
        send_updates = "all" if has_attendees else "none"
        try:
            await self.provider.delete_event(args.event_id, send_updates=send_updates)
        except _DEFINITE as exc:
            return _write_failure(exc)
        return ToolResult.success(
            {"event_id": args.event_id, "cancelled": True, "attendees_notified": has_attendees}
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _range(
        self, start_local: str, end_local: str, timezone: str | None
    ) -> tuple[datetime, datetime, ZoneInfo]:
        zone_name = timezone or self.default_timezone
        start = parse_local(start_local, zone_name).due_at_utc
        end = parse_local(end_local, zone_name).due_at_utc
        return start, end, load_zone(zone_name)

    def _end_for_create(
        self, args: CreateEventArgs, start: datetime, zone: str
    ) -> datetime:
        if args.end_local is not None:
            return parse_local(args.end_local, zone).due_at_utc
        minutes = args.duration_minutes or self.default_duration_minutes
        return start + timedelta(minutes=minutes)

    def _updated_times(
        self, current: dict[str, Any], args: UpdateEventArgs, zone: str
    ) -> tuple[datetime, datetime]:
        start_payload = current.get("start") if isinstance(current.get("start"), dict) else {}
        if "date" in start_payload:
            raise InvalidLocalTime(
                "the event is all-day; this tool only edits timed events",
                reason="all_day",
            )
        existing_start = _event_start_instant(current)
        existing_end = _event_end_instant(current)
        if existing_start is None or existing_end is None:
            raise InvalidLocalTime(
                "the event has no dateTime; all-day events are not editable here",
                reason="all_day",
            )
        if args.start_local is not None:
            new_start = parse_local(args.start_local, zone).due_at_utc
        else:
            new_start = existing_start
        if args.end_local is not None:
            new_end = parse_local(args.end_local, zone).due_at_utc
        elif args.duration_minutes is not None:
            new_end = new_start + timedelta(minutes=args.duration_minutes)
        elif args.start_local is not None:
            new_end = new_start + (existing_end - existing_start)
        else:
            new_end = existing_end
        return new_start, new_end


def specs(tools: CalendarTools) -> list[ToolSpec]:
    return [
        ToolSpec(
            name="list_calendar_events",
            description=(
                "List the user's calendar events in a local date range. Returns event ids, "
                "summaries, start/end, attendees and links. Use get_current_datetime first for "
                "relative dates."
            ),
            args_model=ListEventsArgs,
            risk_level=RiskLevel.READ,
            timeout_seconds=15.0,
            handler=tools.list_events,
        ),
        ToolSpec(
            name="get_calendar_event",
            description=(
                "Read one calendar event by id, including its summary, times, attendees and "
                "description excerpt."
            ),
            args_model=GetEventArgs,
            risk_level=RiskLevel.READ,
            timeout_seconds=15.0,
            handler=tools.get_event,
        ),
        ToolSpec(
            name="find_free_slots",
            description=(
                "Find free slots of a given duration in a local date range, within working hours. "
                "Use it before proposing a meeting time."
            ),
            args_model=FreeSlotsArgs,
            risk_level=RiskLevel.READ,
            timeout_seconds=20.0,
            handler=tools.find_free_slots,
        ),
        ToolSpec(
            name="create_calendar_event",
            description=(
                "Prepare a new calendar event. Requires the user's explicit approval before it "
                "runs; the preview shows title, time, location, attendees and Meet link."
            ),
            args_model=CreateEventArgs,
            risk_level=RiskLevel.EXTERNAL,
            timeout_seconds=20.0,
            handler=tools.create_event,
        ),
        ToolSpec(
            name="update_calendar_event",
            description=(
                "Prepare a change to an existing calendar event. Read it first and pass its "
                "summary/start as expected_*; requires approval."
            ),
            args_model=UpdateEventArgs,
            risk_level=RiskLevel.EXTERNAL,
            timeout_seconds=20.0,
            handler=tools.update_event,
        ),
        ToolSpec(
            name="cancel_calendar_event",
            description=(
                "Prepare a cancellation of an existing calendar event. Read it first and pass "
                "its summary/start as expected_*; requires approval."
            ),
            args_model=CancelEventArgs,
            risk_level=RiskLevel.EXTERNAL,
            timeout_seconds=20.0,
            handler=tools.cancel_event,
        ),
    ]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _missing_provider() -> ToolResult[Any]:
    return ToolResult.failure(
        "CapabilityUnavailable",
        message_key="error.capability_unavailable",
        detail="Google Calendar is not configured",
        capability="google_calendar",
    )


def _invalid_time(exc: InvalidLocalTime) -> ToolResult[Any]:
    return ToolResult.failure(
        "invalid_local_time",
        message_key="error.invalid_due_time",
        detail=exc.detail,
        reason=exc.reason,
    )


def _limit() -> ToolResult[Any]:
    return ToolResult.failure(
        "calendar_limit",
        message_key="error.calendar_limit",
        detail="the request exceeds the configured calendar limits",
    )


def _read_failure(exc: GoogleError) -> ToolResult[Any]:
    return ToolResult.failure(exc.code, message_key=exc.message_key, detail=exc.detail)


def _write_failure(exc: GoogleError) -> ToolResult[Any]:
    return ToolResult.failure(exc.code, message_key=exc.message_key, detail=exc.detail)


def _rfc3339(instant: datetime) -> str:
    return instant.isoformat().replace("+00:00", "Z")


def _event_id(args: CreateEventArgs, context: dict[str, Any] | None) -> str:
    action_id = str((context or {}).get("action_id") or "")
    if action_id:
        cleaned = re.sub(r"[^a-v0-9]", "", action_id.lower())
        if cleaned:
            return ("taa" + cleaned)[:_MAX_EVENT_ID_CHARS]
    payload = f"{args.summary}|{args.start_local}|{args.end_local}|{args.duration_minutes}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"taa{digest}"


def _meet_request(key: str) -> dict[str, Any]:
    return {
        "createRequest": {
            "requestId": f"taa-{key[:40]}",
            "conferenceSolutionKey": {"type": "hangoutsMeet"},
        }
    }


def _normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    start = event.get("start") if isinstance(event.get("start"), dict) else {}
    end = event.get("end") if isinstance(event.get("end"), dict) else {}
    all_day = "date" in start
    description = str(event.get("description") or "")
    attendees = []
    for attendee in (event.get("attendees") or [])[:20]:
        if isinstance(attendee, dict):
            attendees.append(
                {
                    "email": attendee.get("email"),
                    "response_status": attendee.get("responseStatus"),
                    "optional": bool(attendee.get("optional", False)),
                }
            )
    return {
        "event_id": event.get("id"),
        "summary": event.get("summary") or "",
        "status": event.get("status"),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "all_day": all_day,
        "timezone": start.get("timeZone") or end.get("timeZone"),
        "location": event.get("location") or "",
        "description": description[:_MAX_RETURNED_DESCRIPTION],
        "description_truncated": len(description) > _MAX_RETURNED_DESCRIPTION,
        "attendees": attendees,
        "meet_link": event.get("hangoutLink") or _conference_link(event),
        "html_link": event.get("htmlLink"),
    }


def _conference_link(event: dict[str, Any]) -> str | None:
    conference = event.get("conferenceData")
    if not isinstance(conference, dict):
        return None
    for entry in conference.get("entryPoints") or []:
        if isinstance(entry, dict) and entry.get("entryPointType") == "video":
            return entry.get("uri")
    return None


def _event_start_text(event: dict[str, Any]) -> str:
    start = event.get("start") if isinstance(event.get("start"), dict) else {}
    return str(start.get("dateTime") or start.get("date") or "")


def _matches_expected(event: dict[str, Any], summary: str, expected_start: str) -> bool:
    if str(event.get("summary") or "") != summary:
        return False
    return _same_instant(_event_start_text(event), expected_start)


def _same_event(event: dict[str, Any], *, summary: str, start: str) -> bool:
    return str(event.get("summary") or "") == summary and _same_instant(
        _event_start_text(event), start
    )


def _same_instant(left: str, right: str) -> bool:
    if left == right:
        return True
    try:
        left_dt = datetime.fromisoformat(left.replace("Z", "+00:00"))
        right_dt = datetime.fromisoformat(right.replace("Z", "+00:00"))
    except ValueError:
        return left == right
    return left_dt == right_dt


def _event_start_instant(event: dict[str, Any]) -> datetime | None:
    text = _event_start_text(event)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _event_end_instant(event: dict[str, Any]) -> datetime | None:
    end = event.get("end") if isinstance(event.get("end"), dict) else {}
    text = str(end.get("dateTime") or end.get("date") or "")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _writable_event(event: dict[str, Any]) -> dict[str, Any]:
    allowed = ("summary", "description", "location", "start", "end", "attendees", "reminders")
    return {key: event[key] for key in allowed if key in event}


def _parse_busy(items: list[dict[str, Any]]) -> list[tuple[datetime, datetime]]:
    busy: list[tuple[datetime, datetime]] = []
    for item in items:
        try:
            start = datetime.fromisoformat(str(item["start"]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(item["end"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        busy.append((start, end))
    return sorted(busy)


def _free_slots(
    *,
    start: datetime,
    end: datetime,
    busy: list[tuple[datetime, datetime]],
    zone: ZoneInfo,
    duration: timedelta,
    work_start_hour: int,
    work_end_hour: int,
    include_weekends: bool,
) -> list[dict[str, str]]:
    slots: list[dict[str, str]] = []
    day: date = start.astimezone(zone).date()
    last_day = end.astimezone(zone).date()
    while day <= last_day:
        if day.weekday() < 5 or include_weekends:
            window_start = datetime.combine(day, time(hour=work_start_hour), tzinfo=zone)
            if work_end_hour == 24:
                window_end = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=zone)
            else:
                window_end = datetime.combine(day, time(hour=work_end_hour), tzinfo=zone)
            window_start = max(window_start, start)
            window_end = min(window_end, end)
            if window_end > window_start:
                slots.extend(
                    _gaps(window_start, window_end, busy, duration, zone)
                )
                if len(slots) > _MAX_SLOTS:
                    return slots
        day += timedelta(days=1)
    return slots


def _gaps(
    window_start: datetime,
    window_end: datetime,
    busy: list[tuple[datetime, datetime]],
    duration: timedelta,
    zone: ZoneInfo,
) -> list[dict[str, str]]:
    gaps: list[dict[str, str]] = []
    cursor = window_start
    for busy_start, busy_end in busy:
        if busy_end <= window_start or busy_start >= window_end:
            continue
        if busy_start > cursor and busy_start - cursor >= duration:
            gaps.append(_slot(cursor, busy_start, zone))
        cursor = max(cursor, busy_end)
        if cursor >= window_end:
            break
    if window_end - cursor >= duration:
        gaps.append(_slot(cursor, window_end, zone))
    return gaps


def _slot(start: datetime, end: datetime, zone: ZoneInfo) -> dict[str, str]:
    local_start = start.astimezone(zone)
    local_end = end.astimezone(zone)
    return {
        "start": local_start.isoformat(),
        "end": local_end.isoformat(),
        "start_local": describe_instant(start, zone.key),
        "end_local": describe_instant(end, zone.key),
    }


__all__ = [
    "CalendarTools",
    "CancelEventArgs",
    "CreateEventArgs",
    "FreeSlotsArgs",
    "GetEventArgs",
    "ListEventsArgs",
    "UpdateEventArgs",
    "specs",
]
