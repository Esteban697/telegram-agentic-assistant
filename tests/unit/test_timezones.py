"""Timezone and due-date resolution (PLAN.md §9.1 "Conversión de fechas y zonas horarias")."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.policy.timezones import (
    InvalidLocalTime,
    describe_instant,
    parse_local,
    resolve_local,
)


def test_a_normal_local_time_resolves_to_the_right_instant() -> None:
    resolved = parse_local("2026-11-03 09:30", "Europe/Madrid")
    # November is CET (UTC+1).
    assert resolved.due_at_utc == datetime(2026, 11, 3, 8, 30, tzinfo=UTC)
    assert resolved.timezone == "Europe/Madrid"


def test_summer_local_time_uses_the_summer_offset() -> None:
    resolved = parse_local("2026-07-15 09:30", "Europe/Madrid")
    assert resolved.due_at_utc == datetime(2026, 7, 15, 7, 30, tzinfo=UTC)  # CEST, UTC+2


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        # Europe/Madrid 2026: clocks go forward on 29 March and back on 25 October.
        ("2026-03-29 02:30", "non_existent"),
        ("2026-10-25 02:30", "ambiguous"),
    ],
)
def test_dst_edges_are_rejected_with_distinct_reasons(text: str, reason: str) -> None:
    """Neither edge may be silently shifted: one time does not exist, the other happens twice."""

    with pytest.raises(InvalidLocalTime) as excinfo:
        parse_local(text, "Europe/Madrid")
    assert excinfo.value.reason == reason


def test_the_instants_around_the_spring_gap_still_work() -> None:
    before = parse_local("2026-03-29 01:59", "Europe/Madrid")
    after = parse_local("2026-03-29 03:00", "Europe/Madrid")
    assert before.due_at_utc < after.due_at_utc
    assert after.due_at_utc - before.due_at_utc == __import__("datetime").timedelta(minutes=1)


def test_the_instants_around_the_autumn_overlap_still_work() -> None:
    assert parse_local("2026-10-25 01:59", "Europe/Madrid").due_at_utc < parse_local(
        "2026-10-25 03:00", "Europe/Madrid"
    ).due_at_utc


def test_an_ambiguous_time_in_another_zone_is_fine() -> None:
    """The rejection is a property of the zone, not of the date: UTC has no DST at all."""

    assert parse_local("2026-10-25 02:30", "UTC").due_at_utc == datetime(
        2026, 10, 25, 2, 30, tzinfo=UTC
    )


def test_an_unknown_zone_is_rejected() -> None:
    with pytest.raises(InvalidLocalTime) as excinfo:
        parse_local("2026-11-03 09:30", "Mars/Olympus")
    assert excinfo.value.reason == "unknown_zone"


def test_an_unparsable_date_is_rejected() -> None:
    with pytest.raises(InvalidLocalTime) as excinfo:
        parse_local("mañana por la tarde", "Europe/Madrid")
    assert excinfo.value.reason == "unparsable"


def test_an_instant_with_an_offset_is_rejected() -> None:
    with pytest.raises(InvalidLocalTime) as excinfo:
        resolve_local(datetime(2026, 11, 3, 9, 30, tzinfo=UTC), "Europe/Madrid")
    assert excinfo.value.reason == "unexpected_offset"


def test_accepts_iso_and_slash_forms() -> None:
    iso = parse_local("2026-11-03T09:30", "Europe/Madrid")
    slashed = parse_local("03/11/2026 09:30", "Europe/Madrid")
    assert iso.due_at_utc == slashed.due_at_utc


def test_describe_instant_renders_in_the_target_zone() -> None:
    instant = datetime(2026, 11, 3, 8, 30, tzinfo=UTC)
    assert describe_instant(instant, "Europe/Madrid") == "03/11/2026 09:30"
    assert describe_instant(instant, "UTC") == "03/11/2026 08:30"
