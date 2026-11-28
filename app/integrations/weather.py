"""Open-Meteo weather for the morning brief (layer 1: an external HTTP adapter).

Open-Meteo is used because it needs **no API key and no account**: the brief must not add
another secret to rotate. The WMO weather code crosses this boundary unchanged; its Spanish
description lives in the catalogue (``app/bot/messages.py``), never here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


class WeatherUnavailable(Exception):
    """The forecast could not be fetched or parsed.

    The brief treats this as "no weather today", not as a failure: a job that fails because a
    third party is slow would also skip the agenda and the reminders.
    """


@dataclass(frozen=True)
class WeatherSnapshot:
    temperature: float
    apparent: float | None
    minimum: float | None
    maximum: float | None
    precipitation_probability: int | None
    code: int | None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first(payload: Any) -> Any:
    """Open-Meteo returns daily aggregates as one-element arrays (``forecast_days=1``)."""

    if isinstance(payload, list):
        return payload[0] if payload else None
    return payload


async def fetch_weather(
    *,
    latitude: float,
    longitude: float,
    timezone: str,
    timeout: float = 10.0,
    client: httpx.AsyncClient | None = None,
) -> WeatherSnapshot:
    """Fetch today's current conditions and the daily min/max.

    ``client`` exists for the tests (``httpx.MockTransport``); production passes ``None`` and
    this function owns (and closes) the client.
    """

    params = {
        "latitude": f"{latitude}",
        "longitude": f"{longitude}",
        "current": "temperature_2m,apparent_temperature,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": timezone,
        "forecast_days": "1",
    }

    owns_client = client is None
    active = client or httpx.AsyncClient(timeout=timeout)
    try:
        try:
            response = await active.get(OPEN_METEO_URL, params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise WeatherUnavailable("open-meteo request failed") from exc
    finally:
        if owns_client:
            await active.aclose()

    if not isinstance(payload, dict):
        raise WeatherUnavailable("open-meteo returned a non-object payload")
    current = payload.get("current")
    if not isinstance(current, dict):
        raise WeatherUnavailable("open-meteo payload has no 'current' object")

    temperature = _number(current.get("temperature_2m"))
    if temperature is None:
        raise WeatherUnavailable("open-meteo payload has no current temperature")

    daily = payload.get("daily") if isinstance(payload.get("daily"), dict) else {}
    precipitation = _number(_first(daily.get("precipitation_probability_max")))
    code = _number(current.get("weather_code"))

    return WeatherSnapshot(
        temperature=temperature,
        apparent=_number(current.get("apparent_temperature")),
        minimum=_number(_first(daily.get("temperature_2m_min"))),
        maximum=_number(_first(daily.get("temperature_2m_max"))),
        precipitation_probability=int(precipitation) if precipitation is not None else None,
        code=int(code) if code is not None else None,
    )


__all__ = ["OPEN_METEO_URL", "WeatherSnapshot", "WeatherUnavailable", "fetch_weather"]
