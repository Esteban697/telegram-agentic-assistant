"""Google Calendar REST adapter (user OAuth 2.0, installed-app flow).

This is the only module that talks to Google's token and Calendar endpoints. It is deliberately
small and uses ``httpx`` (already a dependency) instead of adding the Google API client: the
request shapes are a handful of documented REST calls, and an injectable ``httpx.AsyncClient``
keeps the unit tests network-free.

Credential handling:

* the refresh token, client id and client secret live in one JSON document produced by
  ``scripts/google_auth.py`` and mounted from Secret Manager;
* access tokens are cached in memory only, refreshed 60 s before expiry under an
  ``asyncio.Lock``;
* nothing here logs a token, a refresh token, an event description or an attendee email.

Failures are typed (``app.errors``) so the tool layer can decide whether an outcome is definite
(a write did not happen) or ambiguous (the turn is recorded as ``unknown``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable
from urllib.parse import quote

import httpx

from app.config import Settings
from app.errors import (
    CalendarEventNotFound,
    GoogleAuthError,
    GoogleBadRequest,
    GoogleBadResponse,
    GoogleConflict,
    GoogleRateLimited,
    GoogleTimeout,
    GoogleUnavailable,
)

_logger = logging.getLogger(__name__)

CALENDAR_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.events.freebusy",
)
TOKEN_URI = "https://oauth2.googleapis.com/token"
CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"

_RATE_REASONS = frozenset(
    {
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "quotaExceeded",
        "dailyLimitExceeded",
    }
)


@dataclass(frozen=True, slots=True)
class GoogleOAuthCredentials:
    """The credentials document written by ``scripts/google_auth.py``."""

    client_id: str
    client_secret: str
    refresh_token: str
    token_uri: str = TOKEN_URI
    scopes: tuple[str, ...] = CALENDAR_SCOPES


def parse_credentials(raw: str) -> GoogleOAuthCredentials:
    """Parse and validate the combined credentials JSON. Never echo the input on failure."""

    if not raw.strip():
        raise ValueError("GOOGLE_OAUTH_CREDENTIALS is empty")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("GOOGLE_OAUTH_CREDENTIALS is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("GOOGLE_OAUTH_CREDENTIALS must be a JSON object")

    def required(name: str) -> str:
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"GOOGLE_OAUTH_CREDENTIALS is missing {name!r}")
        return value.strip()

    scopes = payload.get("scopes")
    if not isinstance(scopes, list) or not all(isinstance(item, str) for item in scopes):
        scopes = list(CALENDAR_SCOPES)

    return GoogleOAuthCredentials(
        client_id=required("client_id"),
        client_secret=required("client_secret"),
        refresh_token=required("refresh_token"),
        token_uri=str(payload.get("token_uri") or TOKEN_URI),
        scopes=tuple(scopes),
    )


class OAuthTokenCache:
    """Refresh-token exchange with an in-memory access-token cache and a refresh lock."""

    def __init__(
        self,
        credentials: GoogleOAuthCredentials,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        refresh_margin: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._credentials = credentials
        self._timeout = timeout
        self._refresh_margin = refresh_margin
        self._clock = clock
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout))
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def token(self) -> str:
        async with self._lock:
            if self._token is not None and self._clock() < self._expires_at - self._refresh_margin:
                return self._token
            await self._refresh_locked()
            assert self._token is not None
            return self._token

    async def _refresh_locked(self) -> None:
        body = {
            "client_id": self._credentials.client_id,
            "client_secret": self._credentials.client_secret,
            "refresh_token": self._credentials.refresh_token,
            "grant_type": "refresh_token",
        }
        try:
            response = await self._client.post(
                self._credentials.token_uri,
                data=body,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise GoogleTimeout("Google token refresh timed out") from exc
        except httpx.TransportError as exc:
            raise GoogleUnavailable("Google token refresh transport failure") from exc

        status = response.status_code
        if status == 429:
            raise GoogleRateLimited("Google token refresh was rate limited")
        if status in {400, 401, 403}:
            # invalid_grant: revoked/expired refresh token, wrong client, or missing consent.
            raise GoogleAuthError(
                "Google rejected the refresh token; re-run scripts/google_auth.py and redeploy"
            )
        if status >= 500:
            raise GoogleUnavailable(f"Google token refresh returned status {status}")
        if status >= 400:
            raise GoogleAuthError(f"Google token refresh returned status {status}")

        try:
            payload = response.json()
            token = payload["access_token"]
            expires_in = int(payload.get("expires_in") or 3600)
        except (KeyError, TypeError, ValueError) as exc:
            raise GoogleBadResponse("Google token refresh returned an unexpected shape") from exc
        if not isinstance(token, str) or not token:
            raise GoogleBadResponse("Google token refresh returned no access_token")

        self._token = token
        self._expires_at = self._clock() + max(1, expires_in)
        _logger.info(
            "google access token refreshed",
            extra={"expires_in": expires_in, "status": "ok"},
        )


class GoogleCalendarProvider:
    """Async port used by the Calendar tools."""

    def __init__(
        self,
        *,
        credentials: GoogleOAuthCredentials,
        calendar_id: str = "primary",
        timeout: float = 10.0,
        max_results: int = 50,
        calendar_base: str = CALENDAR_BASE,
        client: httpx.AsyncClient | None = None,
        token_cache: OAuthTokenCache | None = None,
    ) -> None:
        self.calendar_id = calendar_id
        self._timeout = timeout
        self._max_results = max_results
        self._calendar_base = calendar_base.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout))
        self._tokens = token_cache or OAuthTokenCache(
            credentials, client=self._client, timeout=timeout
        )
        self._owns_tokens = token_cache is None

    async def aclose(self) -> None:
        if self._owns_tokens:
            await self._tokens.aclose()
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ #
    # Calendar operations
    # ------------------------------------------------------------------ #
    async def list_events(
        self,
        *,
        time_min: datetime,
        time_max: datetime,
        query: str | None = None,
        max_results: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {
            "timeMin": _rfc3339(time_min),
            "timeMax": _rfc3339(time_max),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": str(min(max_results or self._max_results, self._max_results)),
        }
        if query:
            params["q"] = query
        payload = await self._request(
            "GET", f"/calendars/{quote(self.calendar_id, safe='')}/events", params=params
        )
        items = payload.get("items")
        if not isinstance(items, list):
            raise GoogleBadResponse("Calendar list response had no 'items' array")
        return [item for item in items if isinstance(item, dict)]

    async def get_event(self, event_id: str) -> dict[str, Any]:
        payload = await self._request(
            "GET",
            f"/calendars/{quote(self.calendar_id, safe='')}/events/{quote(event_id, safe='')}",
            event_scoped=True,
        )
        return payload

    async def create_event(
        self, body: dict[str, Any], *, send_updates: str
    ) -> dict[str, Any]:
        params = {"sendUpdates": send_updates}
        if "conferenceData" in body:
            params["conferenceDataVersion"] = "1"
        return await self._request(
            "POST",
            f"/calendars/{quote(self.calendar_id, safe='')}/events",
            params=params,
            json_body=body,
        )

    async def update_event(
        self, event_id: str, body: dict[str, Any], *, send_updates: str
    ) -> dict[str, Any]:
        params = {"sendUpdates": send_updates}
        if "conferenceData" in body:
            params["conferenceDataVersion"] = "1"
        return await self._request(
            "PUT",
            f"/calendars/{quote(self.calendar_id, safe='')}/events/{quote(event_id, safe='')}",
            params=params,
            json_body=body,
            event_scoped=True,
        )

    async def delete_event(self, event_id: str, *, send_updates: str) -> None:
        try:
            await self._request(
                "DELETE",
                f"/calendars/{quote(self.calendar_id, safe='')}/events/{quote(event_id, safe='')}",
                params={"sendUpdates": send_updates},
                event_scoped=True,
            )
        except CalendarEventNotFound:
            # Deleting an already-deleted event is idempotent success.
            return

    async def free_busy(
        self, *, time_min: datetime, time_max: datetime, time_zone: str
    ) -> list[dict[str, str]]:
        payload = await self._request(
            "POST",
            "/freeBusy",
            json_body={
                "timeMin": _rfc3339(time_min),
                "timeMax": _rfc3339(time_max),
                "timeZone": time_zone,
                "items": [{"id": self.calendar_id}],
            },
        )
        calendars = payload.get("calendars")
        if not isinstance(calendars, dict):
            raise GoogleBadResponse("freeBusy response had no 'calendars' object")
        entry = calendars.get(self.calendar_id)
        if not isinstance(entry, dict):
            raise GoogleBadResponse("freeBusy response did not include the requested calendar")
        errors = entry.get("errors")
        if errors:
            reasons = ", ".join(
                str(item.get("reason")) for item in errors if isinstance(item, dict)
            )
            raise GoogleBadRequest(f"freeBusy returned errors: {reasons}")
        busy = entry.get("busy")
        if not isinstance(busy, list):
            raise GoogleBadResponse("freeBusy response had no 'busy' array")
        return [item for item in busy if isinstance(item, dict)]

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        event_scoped: bool = False,
    ) -> dict[str, Any]:
        token = await self._tokens.token()
        headers = {"Authorization": f"Bearer {token}"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = await self._client.request(
                method,
                f"{self._calendar_base}{path}",
                params=params,
                json=json_body,
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise GoogleTimeout(f"{method} {path} timed out") from exc
        except httpx.TransportError as exc:
            raise GoogleUnavailable(f"{method} {path} transport failure") from exc

        status = response.status_code
        if status == 429:
            raise GoogleRateLimited(f"{method} {path} rate limited")
        if status == 401:
            raise GoogleAuthError("Calendar API rejected the access token")
        if status == 403:
            reason = _error_reason(response)
            if reason in _RATE_REASONS:
                raise GoogleRateLimited(f"{method} {path} quota exceeded")
            raise GoogleBadRequest(f"{method} {path} returned 403")
        if status == 404:
            if event_scoped:
                raise CalendarEventNotFound(f"{method} {path} returned 404")
            raise GoogleBadRequest(f"{method} {path} returned 404")
        if status == 409:
            raise GoogleConflict(f"{method} {path} returned 409")
        if status == 400:
            raise GoogleBadRequest(f"{method} {path} returned 400")
        if status >= 500:
            raise GoogleUnavailable(f"{method} {path} returned {status}")
        if status >= 400:
            raise GoogleBadRequest(f"{method} {path} returned {status}")
        if status == 204 or not response.content:
            return {}
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise GoogleBadResponse(f"{method} {path} returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise GoogleBadResponse(f"{method} {path} returned a non-object")
        return payload


def _rfc3339(instant: datetime) -> str:
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _error_reason(response: httpx.Response) -> str:
    """Return the provider's reason token, never the body text."""

    try:
        payload = response.json()
        errors = payload.get("error", {}).get("errors", [])
        if errors and isinstance(errors, list) and isinstance(errors[0], dict):
            return str(errors[0].get("reason") or "")
    except (AttributeError, TypeError, ValueError):
        pass
    return ""


def build_calendar_provider(settings: Settings) -> GoogleCalendarProvider | None:
    """Construct the provider when the integration is enabled; ``None`` otherwise."""

    if not settings.google_calendar_enabled:
        return None
    credentials = parse_credentials(settings.google_oauth_credentials.get_secret_value())
    return GoogleCalendarProvider(
        credentials=credentials,
        calendar_id=settings.google_calendar_id,
        timeout=settings.google_calendar_timeout_seconds,
        max_results=settings.google_calendar_max_results,
    )


__all__ = [
    "CALENDAR_SCOPES",
    "CALENDAR_BASE",
    "GoogleCalendarProvider",
    "GoogleOAuthCredentials",
    "OAuthTokenCache",
    "TOKEN_URI",
    "build_calendar_provider",
    "parse_credentials",
]
