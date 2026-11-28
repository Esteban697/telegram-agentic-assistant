"""Render the morning brief (Spanish text lives in the catalogue, never here).

The composer takes plain data — no provider, no database, no HTTP — so every branch (agenda
present, calendar unconfigured, expired authorization, weather missing) is a unit test.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from app.integrations.weather import WeatherSnapshot

from . import messages

CALENDAR_OK: Final = "ok"
CALENDAR_DISABLED: Final = "disabled"
CALENDAR_AUTH: Final = "auth"
CALENDAR_UNAVAILABLE: Final = "unavailable"


@dataclass(frozen=True)
class AgendaEntry:
    summary: str
    start: datetime | None  # ``None`` means an all-day event (Google reports a date, not a time)
    location: str | None = None


@dataclass(frozen=True)
class ReminderEntry:
    title: str
    due_at: datetime


def _number(value: float | None) -> str | None:
    """Temperatures are shown as whole degrees: 12.4 °C is noise in a one-line brief."""

    if value is None:
        return None
    return str(int(round(value)))


def _agenda_lines(agenda: list[AgendaEntry], calendar_state: str) -> list[str]:
    if calendar_state == CALENDAR_DISABLED:
        # Not a failure: the calendar is an optional integration, so the brief simply omits it.
        return []
    if calendar_state == CALENDAR_AUTH:
        return [messages.render("brief.agenda.auth")]
    if calendar_state == CALENDAR_UNAVAILABLE:
        return [messages.render("brief.agenda.unavailable")]
    if not agenda:
        return [messages.render("brief.agenda.empty")]

    lines = [messages.render("brief.agenda.title", count=len(agenda))]
    for entry in agenda:
        if entry.start is None:
            lines.append(messages.render("brief.agenda.item_all_day", summary=entry.summary))
            continue
        when = entry.start.strftime("%H:%M")
        if entry.location:
            lines.append(
                messages.render(
                    "brief.agenda.item_location",
                    time=when,
                    summary=entry.summary,
                    location=entry.location,
                )
            )
        else:
            lines.append(
                messages.render("brief.agenda.item", time=when, summary=entry.summary)
            )
    return lines


def _reminder_lines(reminders: list[ReminderEntry] | None) -> list[str]:
    if reminders is None:
        # `None` is "could not read them"; `[]` is "none due today". They must not look alike.
        return [messages.render("brief.reminders.unavailable")]
    if not reminders:
        return [messages.render("brief.reminders.empty")]
    lines = [messages.render("brief.reminders.title", count=len(reminders))]
    for entry in reminders:
        lines.append(
            messages.render(
                "brief.reminders.item",
                time=entry.due_at.strftime("%H:%M"),
                title=entry.title,
            )
        )
    return lines


def _weather_line(city: str, weather: WeatherSnapshot | None) -> list[str]:
    if weather is None:
        # Silence beats "no data": the brief must not open with four lines of apologies.
        return []
    description = messages.weather_description(weather.code)
    temperature = _number(weather.temperature)
    if temperature is None:
        return []

    apparent = _number(weather.apparent)
    minimum = _number(weather.minimum)
    maximum = _number(weather.maximum)
    if apparent is not None and minimum is not None and maximum is not None:
        return [
            messages.render(
                "brief.weather.line",
                city=city,
                temperature=temperature,
                apparent=apparent,
                minimum=minimum,
                maximum=maximum,
                precipitation=(
                    weather.precipitation_probability
                    if weather.precipitation_probability is not None
                    else "—"
                ),
            )
        ]
    if description is None:
        return [
            messages.render("brief.weather.temperature", city=city, temperature=temperature)
        ]
    return [
        messages.render(
            "brief.weather.simple",
            city=city,
            temperature=temperature,
            description=description,
        )
    ]


def _spend_line(spend: tuple[float, str] | None) -> list[str]:
    if spend is None:
        return []
    amount, currency = spend
    return [messages.render("brief.spend.line", amount=f"{amount:.2f}", currency=currency)]


def compose_brief(
    *,
    now: datetime,
    city: str,
    agenda: list[AgendaEntry],
    calendar_state: str = CALENDAR_OK,
    reminders: list[ReminderEntry] | None,
    weather: WeatherSnapshot | None = None,
    spend: tuple[float, str] | None = None,
) -> str:
    """Build the Telegram message. Every section degrades on its own."""

    # Chronology is a presentation decision, so it lives here: all-day entries first, then by
    # start time. The caller should not have to know that the brief reads better that way.
    ordered = sorted(
        agenda,
        key=lambda entry: (entry.start is not None, entry.start.timestamp() if entry.start else 0.0),
    )

    blocks: list[list[str]] = [
        [
            messages.render(
                "brief.header",
                weekday=messages.WEEKDAYS_ES[now.weekday()],
                day=now.day,
                month=messages.MONTHS_ES[now.month - 1],
                year=now.year,
            )
        ],
        _agenda_lines(ordered, calendar_state),
        _reminder_lines(reminders),
        _weather_line(city, weather),
        _spend_line(spend),
    ]
    return "\n\n".join("\n".join(block) for block in blocks if block)


__all__ = [
    "CALENDAR_AUTH",
    "CALENDAR_DISABLED",
    "CALENDAR_OK",
    "CALENDAR_UNAVAILABLE",
    "AgendaEntry",
    "ReminderEntry",
    "compose_brief",
]
