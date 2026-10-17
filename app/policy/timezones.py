"""Timezone and due-date resolution (spec §11, PLAN.md Risks item 12).

Layer 0 and **pure**: no I/O, no clock. Due dates are stored as absolute instants with the
originating IANA zone recorded alongside, and a local time the user supplies is resolved here.

The policy for the two DST edge cases is explicit rather than silent:

* a **non-existent** local time (the spring-forward gap in `Europe/Madrid`, e.g. 2026-03-29
  02:30) is rejected — there is no instant that means it;
* an **ambiguous** local time (the autumn overlap, e.g. 2026-10-25 02:30, which happens twice)
  is rejected too, because picking one silently would schedule the reminder an hour off from
  what the user meant.

Both produce a typed error the bot turns into a clarification request, which is the behaviour
the spec asks for ("Definir zona horaria explícita", "Manejar horario de verano").

*Addition to PLAN.md §2.1's tree, disclosed in the phase report:* the helpers are pure policy
and two layers need them (the tools and their tests), so they live in `policy/` rather than
inside a tool module. Phase 3's registry imports from here unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MADRID = "Europe/Madrid"


class InvalidLocalTime(ValueError):
    """The supplied local time does not exist, or exists twice, in the given zone."""

    code = "invalid_local_time"

    def __init__(self, detail: str, *, reason: str) -> None:
        super().__init__(detail)
        self.detail = detail
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ResolvedDue:
    """A due date: the absolute instant, the zone it was expressed in, and its local text."""

    due_at_utc: datetime
    timezone: str
    local_text: str


def load_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidLocalTime(f"unknown IANA timezone: {name!r}", reason="unknown_zone") from exc


def _is_ambiguous(naive: datetime, zone: ZoneInfo) -> bool:
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    return first.utcoffset() != second.utcoffset()


def _is_non_existent(naive: datetime, zone: ZoneInfo) -> bool:
    """Detect a local time inside a spring-forward gap by round-tripping through UTC."""

    candidate = naive.replace(tzinfo=zone, fold=0)
    round_trip = candidate.astimezone(UTC).astimezone(zone)
    return round_trip.replace(tzinfo=None) != naive


def resolve_local(naive: datetime, timezone: str) -> ResolvedDue:
    """Resolve a naive local time in an IANA zone to an absolute instant."""

    if naive.tzinfo is not None:
        raise InvalidLocalTime(
            "due dates are supplied as a local time plus a timezone, not as an instant",
            reason="unexpected_offset",
        )

    zone = load_zone(timezone)

    if _is_non_existent(naive, zone):
        raise InvalidLocalTime(
            f"{naive.isoformat()} does not exist in {timezone}: the clocks moved forward "
            "across it. Ask for a different time.",
            reason="non_existent",
        )
    if _is_ambiguous(naive, zone):
        raise InvalidLocalTime(
            f"{naive.isoformat()} happens twice in {timezone} (the clocks moved back). "
            "Ask which of the two the user means.",
            reason="ambiguous",
        )

    local = naive.replace(tzinfo=zone)
    return ResolvedDue(
        due_at_utc=local.astimezone(UTC),
        timezone=timezone,
        local_text=local.strftime("%d/%m/%Y %H:%M %Z"),
    )


def parse_local(text: str, timezone: str) -> ResolvedDue:
    """Parse `YYYY-MM-DD HH:MM` (or an ISO form) as a local time in a zone."""

    cleaned = text.strip().replace("T", " ")
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M"):
        try:
            naive = datetime.strptime(cleaned, pattern)
            break
        except ValueError:
            continue
    else:
        raise InvalidLocalTime(
            f"could not read {text!r} as a date and time (expected YYYY-MM-DD HH:MM)",
            reason="unparsable",
        )
    return resolve_local(naive, timezone)


def describe_instant(instant: datetime, timezone: str) -> str:
    """Render an absolute instant in a zone, for a user-facing message."""

    return instant.astimezone(load_zone(timezone)).strftime("%d/%m/%Y %H:%M")


def add_seconds(instant: datetime, seconds: int) -> datetime:
    return instant + timedelta(seconds=seconds)


__all__ = [
    "MADRID",
    "InvalidLocalTime",
    "ResolvedDue",
    "add_seconds",
    "describe_instant",
    "load_zone",
    "parse_local",
    "resolve_local",
]
