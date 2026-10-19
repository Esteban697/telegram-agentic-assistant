"""Google Calendar provider: OAuth refresh, request shapes, errors and redaction.

No network: every test injects ``httpx.MockTransport``. The provider is the only module that
talks to the token and Calendar endpoints, so these tests pin what it sends and what it refuses
to log.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.errors import (
    CalendarEventNotFound,
    GoogleAuthError,
    GoogleBadRequest,
    GoogleConflict,
    GoogleRateLimited,
    GoogleUnavailable,
)
from app.integrations.google_calendar import (
    CALENDAR_SCOPES,
    GoogleCalendarProvider,
    GoogleOAuthCredentials,
    OAuthTokenCache,
    build_calendar_provider,
    parse_credentials,
)
from tests.conftest import make_settings


def _credentials(**overrides: Any) -> GoogleOAuthCredentials:
    values = {
        "client_id": "client-id.apps.googleusercontent.com",
        "client_secret": "client-secret",
        "refresh_token": "refresh-token",
        "token_uri": "https://oauth.invalid/token",
    }
    values.update(overrides)
    return GoogleOAuthCredentials(**values)


def _provider(handler: Any) -> tuple[GoogleCalendarProvider, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = GoogleCalendarProvider(
        credentials=_credentials(),
        calendar_id="primary",
        timeout=2.0,
        max_results=50,
        calendar_base="https://calendar.invalid/calendar/v3",
        client=client,
    )
    return provider, client


def _token_response() -> httpx.Response:
    return httpx.Response(200, json={"access_token": "access-1", "expires_in": 3600})


async def test_token_refresh_is_cached_and_uses_the_refresh_grant() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            seen.append(dict(httpx.QueryParams(request.content.decode())))
            return _token_response()
        return httpx.Response(200, json={"items": []})

    provider, client = _provider(handler)
    try:
        for _ in range(2):
            await provider.list_events(
                time_min=datetime(2026, 1, 1, tzinfo=UTC),
                time_max=datetime(2026, 1, 2, tzinfo=UTC),
            )
    finally:
        await client.aclose()

    assert len(seen) == 1
    assert seen[0]["grant_type"] == "refresh_token"
    assert seen[0]["refresh_token"] == "refresh-token"
    assert seen[0]["client_id"] == "client-id.apps.googleusercontent.com"


async def test_access_token_is_reused_until_the_expiry_margin() -> None:
    calls: list[int] = []
    now = [0.0]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"access_token": f"token-{len(calls)}", "expires_in": 100})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = OAuthTokenCache(_credentials(), client=client, timeout=2.0, clock=lambda: now[0])
    try:
        assert await cache.token() == "token-1"
        now[0] = 30.0
        assert await cache.token() == "token-1"
        now[0] = 50.0  # inside the 60 s refresh margin
        assert await cache.token() == "token-2"
    finally:
        await client.aclose()

    assert len(calls) == 2


async def test_list_events_sends_the_documented_query() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/token"):
            return _token_response()
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "e1",
                        "summary": "Reunión",
                        "start": {"dateTime": "2026-01-01T10:00:00Z"},
                    }
                ]
            },
        )

    provider, client = _provider(handler)
    try:
        events = await provider.list_events(
            time_min=datetime(2026, 1, 1, tzinfo=UTC),
            time_max=datetime(2026, 1, 2, tzinfo=UTC),
            query="reunión",
            max_results=10,
        )
    finally:
        await client.aclose()

    calendar_request = [r for r in seen if r.url.path.endswith("/events")][0]
    assert calendar_request.headers["authorization"] == "Bearer access-1"
    assert calendar_request.url.params["singleEvents"] == "true"
    assert calendar_request.url.params["orderBy"] == "startTime"
    assert calendar_request.url.params["maxResults"] == "10"
    assert calendar_request.url.params["q"] == "reunión"
    assert events[0]["id"] == "e1"


async def test_invalid_grant_is_auth_error_without_echoing_the_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "super-secret"}
        )

    provider, client = _provider(handler)
    try:
        with pytest.raises(GoogleAuthError) as caught:
            await provider.list_events(
                time_min=datetime(2026, 1, 1, tzinfo=UTC),
                time_max=datetime(2026, 1, 2, tzinfo=UTC),
            )
    finally:
        await client.aclose()
    assert "super-secret" not in str(caught.value)


async def test_event_not_found_is_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return _token_response()
        return httpx.Response(404, json={"error": {"message": "not found"}})

    provider, client = _provider(handler)
    try:
        with pytest.raises(CalendarEventNotFound):
            await provider.get_event("missing")
    finally:
        await client.aclose()


async def test_delete_404_is_idempotent_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return _token_response()
        return httpx.Response(404)

    provider, client = _provider(handler)
    try:
        await provider.delete_event("gone", send_updates="none")
    finally:
        await client.aclose()


async def test_conflict_is_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return _token_response()
        return httpx.Response(409)

    provider, client = _provider(handler)
    try:
        with pytest.raises(GoogleConflict):
            await provider.create_event({"id": "x"}, send_updates="none")
    finally:
        await client.aclose()


async def test_429_and_5xx_are_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return _token_response()
        return httpx.Response(429 if request.method == "GET" else 503)

    provider, client = _provider(handler)
    try:
        with pytest.raises(GoogleRateLimited):
            await provider.list_events(
                time_min=datetime(2026, 1, 1, tzinfo=UTC),
                time_max=datetime(2026, 1, 2, tzinfo=UTC),
            )
        with pytest.raises(GoogleUnavailable):
            await provider.create_event({"id": "x"}, send_updates="none")
    finally:
        await client.aclose()


async def test_free_busy_returns_busy_intervals_and_surfaces_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return _token_response()
        return httpx.Response(
            200,
            json={
                "calendars": {
                    "primary": {
                        "busy": [
                            {"start": "2026-01-01T10:00:00Z", "end": "2026-01-01T11:00:00Z"}
                        ]
                    }
                }
            },
        )

    provider, client = _provider(handler)
    try:
        busy = await provider.free_busy(
            time_min=datetime(2026, 1, 1, tzinfo=UTC),
            time_max=datetime(2026, 1, 2, tzinfo=UTC),
            time_zone="Europe/Madrid",
        )
    finally:
        await client.aclose()
    assert busy[0]["start"] == "2026-01-01T10:00:00Z"


async def test_free_busy_calendar_errors_are_bad_requests() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return _token_response()
        return httpx.Response(
            200,
            json={"calendars": {"primary": {"errors": [{"reason": "notFound"}]}}},
        )

    provider, client = _provider(handler)
    try:
        with pytest.raises(GoogleBadRequest):
            await provider.free_busy(
                time_min=datetime(2026, 1, 1, tzinfo=UTC),
                time_max=datetime(2026, 1, 2, tzinfo=UTC),
                time_zone="Europe/Madrid",
            )
    finally:
        await client.aclose()


async def test_provider_never_logs_tokens_or_event_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200, json={"access_token": "SECRET-TOKEN", "expires_in": 3600}
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "e1",
                        "summary": "SECRET-EVENT",
                        "description": "SECRET-DESC",
                    }
                ]
            },
        )

    provider, client = _provider(handler)
    try:
        await provider.list_events(
            time_min=datetime(2026, 1, 1, tzinfo=UTC),
            time_max=datetime(2026, 1, 2, tzinfo=UTC),
        )
    finally:
        await client.aclose()

    assert "SECRET-TOKEN" not in caplog.text
    assert "SECRET-EVENT" not in caplog.text
    assert "SECRET-DESC" not in caplog.text


def test_parse_credentials_validates_required_fields() -> None:
    raw = json.dumps(
        {
            "client_id": "id",
            "client_secret": "secret",
            "refresh_token": "refresh",
            "scopes": list(CALENDAR_SCOPES),
        }
    )
    parsed = parse_credentials(raw)
    assert parsed.client_id == "id"
    assert parsed.scopes == CALENDAR_SCOPES

    with pytest.raises(ValueError):
        parse_credentials("{not json")
    with pytest.raises(ValueError):
        parse_credentials(json.dumps({"client_id": "id"}))


async def test_build_calendar_provider_is_gated_by_the_enabled_flag() -> None:
    disabled = make_settings()
    assert build_calendar_provider(disabled) is None

    enabled = make_settings(
        google_calendar_enabled=True,
        google_oauth_credentials=json.dumps(
            {
                "client_id": "id",
                "client_secret": "secret",
                "refresh_token": "refresh",
                "scopes": list(CALENDAR_SCOPES),
            }
        ),
    )
    provider = build_calendar_provider(enabled)
    assert provider is not None
    assert provider.calendar_id == "primary"
    await provider.aclose()

