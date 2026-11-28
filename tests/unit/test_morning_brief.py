"""Morning brief: composition, weather parsing and agenda normalisation.

No database, no network, no clock: the job's gathering functions take their clients and the
composer takes plain data, so every degradation branch is asserted here rather than discovered
at 07:30.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
import httpx
import pytest

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
from app.integrations.weather import OPEN_METEO_URL, WeatherUnavailable, fetch_weather
from app.jobs.morning_brief import _agenda_entries, _parse_start, _yesterday_spend
from app.services.billing import BillingRows

ZONE = ZoneInfo("Europe/Madrid")
NOW = datetime(2026, 11, 28, 7, 30, tzinfo=ZONE)


# --------------------------------------------------------------------------- #
# Composition
# --------------------------------------------------------------------------- #
def test_header_names_the_spanish_weekday_and_month() -> None:
    text = compose_brief(
        now=NOW, city="Madrid", agenda=[], calendar_state=CALENDAR_OK, reminders=[]
    )

    weekday = messages.WEEKDAYS_ES[NOW.weekday()]
    assert text.startswith(f"Buenos días. Hoy es {weekday} 28 de noviembre de 2026.")


def test_an_event_with_a_location_and_an_all_day_event() -> None:
    agenda = [
        AgendaEntry(summary="Cena de equipo", start=datetime(2026, 11, 28, 20, 0, tzinfo=ZONE), location="Goiko"),
        AgendaEntry(summary="Cumpleaños", start=None),
    ]

    text = compose_brief(
        now=NOW, city="Madrid", agenda=agenda, calendar_state=CALENDAR_OK, reminders=[]
    )

    assert "Agenda de hoy (2):" in text
    assert "todo el día Cumpleaños" in text
    assert "20:00 Cena de equipo — Goiko" in text
    # All-day entries are listed first.
    assert text.index("Cumpleaños") < text.index("Cena de equipo")


def test_an_empty_but_working_calendar_says_so() -> None:
    text = compose_brief(
        now=NOW, city="Madrid", agenda=[], calendar_state=CALENDAR_OK, reminders=[]
    )

    assert "no tienes eventos hoy" in text


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (CALENDAR_AUTH, "autorización de Google ha caducado"),
        (CALENDAR_UNAVAILABLE, "no está disponible"),
    ],
)
def test_a_broken_calendar_degrades_to_a_sentence(state: str, expected: str) -> None:
    text = compose_brief(
        now=NOW, city="Madrid", agenda=[], calendar_state=state, reminders=[]
    )

    assert expected in text
    # The rest of the brief still renders.
    assert "Recordatorios" in text and "Buenos días" in text


def test_a_disabled_calendar_omits_the_section_instead_of_apologising() -> None:
    text = compose_brief(
        now=NOW, city="Madrid", agenda=[], calendar_state=CALENDAR_DISABLED, reminders=[]
    )

    assert "Agenda" not in text


def test_reminders_list_and_both_degradations_look_different() -> None:
    reminders = [
        ReminderEntry(title="Llamar al dentista", due_at=datetime(2026, 11, 28, 9, 30, tzinfo=ZONE))
    ]

    listed = compose_brief(
        now=NOW, city="Madrid", agenda=[], calendar_state=CALENDAR_OK, reminders=reminders
    )
    empty = compose_brief(
        now=NOW, city="Madrid", agenda=[], calendar_state=CALENDAR_OK, reminders=[]
    )
    unavailable = compose_brief(
        now=NOW, city="Madrid", agenda=[], calendar_state=CALENDAR_OK, reminders=None
    )

    assert "09:30 Llamar al dentista" in listed
    assert "ninguno pendiente" in empty
    assert "no he podido consultarlos" in unavailable


def test_weather_full_line_simple_line_and_absent() -> None:
    from app.integrations.weather import WeatherSnapshot

    full = WeatherSnapshot(
        temperature=12.4,
        apparent=11.0,
        minimum=8.1,
        maximum=15.2,
        precipitation_probability=20,
        code=3,
    )
    simple = WeatherSnapshot(
        temperature=12.0,
        apparent=None,
        minimum=None,
        maximum=None,
        precipitation_probability=None,
        code=61,
    )

    with_full = compose_brief(
        now=NOW, city="Madrid", agenda=[], calendar_state=CALENDAR_OK, reminders=[], weather=full
    )
    with_simple = compose_brief(
        now=NOW, city="Madrid", agenda=[], calendar_state=CALENDAR_OK, reminders=[], weather=simple
    )
    without = compose_brief(
        now=NOW, city="Madrid", agenda=[], calendar_state=CALENDAR_OK, reminders=[], weather=None
    )

    assert "Tiempo en Madrid: 12 °C (sensación 11 °C), mín 8 / máx 15, lluvia 20%." in with_full
    assert "Tiempo en Madrid: 12 °C, lluvia ligera." in with_simple
    assert "Tiempo" not in without


def test_yesterday_spend_line() -> None:
    with_spend = compose_brief(
        now=NOW, city="Madrid", agenda=[], calendar_state=CALENDAR_OK, reminders=[], spend=(0.42, "EUR")
    )
    without = compose_brief(
        now=NOW, city="Madrid", agenda=[], calendar_state=CALENDAR_OK, reminders=[], spend=None
    )

    assert "Gasto de ayer: 0.42 EUR." in with_spend
    assert "Gasto" not in without


# --------------------------------------------------------------------------- #
# Weather client
# --------------------------------------------------------------------------- #
PAYLOAD = {
    "current": {"temperature_2m": 12.4, "apparent_temperature": 11.0, "weather_code": 3},
    "daily": {
        "temperature_2m_max": [15.2],
        "temperature_2m_min": [8.1],
        "precipitation_probability_max": [20],
    },
}


def _client(payload: object, status: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(OPEN_METEO_URL)
        return httpx.Response(status, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_weather_parses_current_and_daily_values() -> None:
    client = _client(PAYLOAD)
    try:
        snapshot = await fetch_weather(
            latitude=40.4, longitude=-3.7, timezone="Europe/Madrid", client=client
        )
    finally:
        await client.aclose()

    assert snapshot.temperature == 12.4
    assert snapshot.apparent == 11.0
    assert snapshot.minimum == 8.1
    assert snapshot.maximum == 15.2
    assert snapshot.precipitation_probability == 20
    assert snapshot.code == 3


async def test_weather_without_a_current_temperature_is_unavailable() -> None:
    client = _client({"current": {}, "daily": {}})
    try:
        with pytest.raises(WeatherUnavailable):
            await fetch_weather(
                latitude=40.4, longitude=-3.7, timezone="Europe/Madrid", client=client
            )
    finally:
        await client.aclose()


async def test_weather_http_error_is_unavailable_not_a_crash() -> None:
    client = _client({"reason": "down"}, status=503)
    try:
        with pytest.raises(WeatherUnavailable):
            await fetch_weather(
                latitude=40.4, longitude=-3.7, timezone="Europe/Madrid", client=client
            )
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# Agenda normalisation
# --------------------------------------------------------------------------- #
def test_event_start_handles_utc_suffix_and_date_only() -> None:
    parsed = _parse_start({"dateTime": "2026-11-28T09:00:00Z"}, ZONE)
    assert parsed is not None and parsed.hour == 10  # Madrid is UTC+1 in November
    assert _parse_start({"date": "2026-11-28"}, ZONE) is None
    assert _parse_start({}, ZONE) is None
    assert _parse_start({"dateTime": "not-a-date"}, ZONE) is None


def test_agenda_entries_skip_cancelled_and_name_untitled_events() -> None:
    events = [
        {"summary": "Reunión", "start": {"dateTime": "2026-11-28T09:00:00+01:00"}},
        {"status": "cancelled", "summary": "Cancelado", "start": {"dateTime": "2026-11-28T10:00:00+01:00"}},
        {"start": {"date": "2026-11-28"}},
    ]

    entries = _agenda_entries(events, ZONE)

    assert [entry.summary for entry in entries] == [
        "Reunión",
        messages.render("brief.agenda.untitled"),
    ]
    assert entries[1].start is None  # the date-only event is all-day


# --------------------------------------------------------------------------- #
# Yesterday's spend
# --------------------------------------------------------------------------- #
class FakeBilling:
    project = "telegram-assistant-esteban"

    def __init__(self, rows: list[dict[str, object]] | None) -> None:
        self.rows = rows

    async def fetch_rows(self, *, days: int | None = None) -> BillingRows | None:
        return None if self.rows is None else BillingRows(rows=self.rows, table="t")


async def test_yesterday_spend_sums_only_the_assistant_rows_of_that_day() -> None:
    rows = [
        {"day": "2026-11-27", "project_id": "telegram-assistant-esteban", "currency": "EUR", "net": "0.30"},
        {"day": "2026-11-27", "project_id": "telegram-assistant-esteban", "currency": "EUR", "net": "0.12"},
        {"day": "2026-11-27", "project_id": "otro-proyecto", "currency": "EUR", "net": "9.99"},
        {"day": "2026-11-28", "project_id": "telegram-assistant-esteban", "currency": "EUR", "net": "5.00"},
    ]

    from tests.conftest import make_settings

    result = await _yesterday_spend(
        make_settings(), yesterday="2026-11-27", service=FakeBilling(rows)
    )

    assert result is not None
    assert result[0] == pytest.approx(0.42)
    assert result[1] == "EUR"


async def test_yesterday_spend_is_absent_without_rows_or_export() -> None:
    from tests.conftest import make_settings

    assert await _yesterday_spend(
        make_settings(), yesterday="2026-11-27", service=FakeBilling([])
    ) is None
    assert await _yesterday_spend(
        make_settings(), yesterday="2026-11-27", service=FakeBilling(None)
    ) is None
