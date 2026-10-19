"""Typed error taxonomy.

Layer 0 (PLAN.md §2.2): this module imports nothing from the rest of ``app``.

Every error carries:

* ``code`` — a stable, machine-readable identifier that goes into the structured logs
  (PLAN.md §14 requires "nombre de herramienta, duración, estado y código de error").
* ``message_key`` — the key of a Spanish user-facing string in ``app/bot/messages.py``.

Errors never hold the offending value itself (a token, a message body, a document): only a
short ``detail`` that is safe to log, so that a raised error cannot become a leak.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class AssistantError(Exception):
    """Base class for every error this application raises deliberately."""

    code: str = "internal_error"
    message_key: str = "error.generic"

    def __init__(
        self,
        detail: str = "",
        *,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(detail or self.code)
        self.detail = detail
        self.context: dict[str, Any] = dict(context or {})

    def as_log_fields(self) -> dict[str, Any]:
        """Return only the fields that are safe to put into a log record."""

        return {"error_code": self.code, "error_detail": self.detail, **self.context}

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.detail}" if self.detail else self.code


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
class ConfigError(AssistantError):
    """Settings are missing or invalid. Raised at startup, never per request.

    The message lists *every* problem found in one pass (PLAN.md §8.1: "fail-fast and
    aggregated ... rather than revealing them one deploy at a time").
    """

    code = "config_invalid"
    message_key = "error.generic"


# --------------------------------------------------------------------------- #
# Transport / security
# --------------------------------------------------------------------------- #
class WebhookSecretInvalid(AssistantError):
    """The ``X-Telegram-Bot-Api-Secret-Token`` header is missing or wrong -> 403."""

    code = "webhook_secret_invalid"
    message_key = "error.generic"


class UnauthorisedUser(AssistantError):
    """The update comes from a Telegram user that is not on the allow-list."""

    code = "unauthorised_user"
    message_key = "error.generic"


class RateLimited(AssistantError):
    """The per-user token bucket is empty (PLAN.md §7.6)."""

    code = "rate_limited"
    message_key = "error.rate_limited"


class CapabilityUnavailable(AssistantError):
    """A feature exists in the command surface but is not implemented in this phase.

    Used for ``/tasks``, ``/memory`` and ``/forget`` in Phase 1 so the command surface
    does not change when Phase 2 implements them (PLAN.md §10 Phase 1 deliverable 7).
    """

    code = "capability_unavailable"
    message_key = "error.not_available"


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #
class BudgetExceeded(AssistantError):
    """The turn did not finish inside ``REQUEST_BUDGET_SECONDS`` (NN8)."""

    code = "request_budget_exceeded"
    message_key = "error.timeout"


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
class TelegramError(AssistantError):
    """Transport-level failure talking to the Bot API."""

    code = "telegram_transport_error"
    message_key = "error.generic"


class TelegramApiError(AssistantError):
    """The Bot API answered HTTP 200 with ``ok: false``, or a non-2xx status.

    HTTP 200 with ``ok: false`` is a *logical* failure: reading the transport status as
    success is the classic misuse this class exists to prevent (PLAN.md §10 Phase 1
    deliverable 6).
    """

    code = "telegram_api_error"
    message_key = "error.generic"


class TelegramRateLimited(AssistantError):
    """The Bot API returned 429. ``retry_after`` is the server-provided delay."""

    code = "telegram_rate_limited"
    message_key = "error.generic"

    def __init__(self, detail: str = "", *, retry_after: int | None = None) -> None:
        super().__init__(detail, context={"retry_after": retry_after})
        self.retry_after = retry_after


# --------------------------------------------------------------------------- #
# Ollama
# --------------------------------------------------------------------------- #
class OllamaError(AssistantError):
    """Base class for every provider-side failure from the chat model."""

    code = "ollama_error"
    message_key = "error.model_unavailable"


class OllamaTimeout(OllamaError):
    code = "ollama_timeout"
    message_key = "error.timeout"


class OllamaUnavailable(OllamaError):
    code = "ollama_unavailable"


class OllamaRateLimited(OllamaError):
    code = "ollama_rate_limited"
    message_key = "error.model_unavailable"


class OllamaBadResponse(OllamaError):
    """The response was not the shape the API documents (missing keys, bad JSON)."""

    code = "ollama_bad_response"


class OllamaUnsupportedModel(OllamaError):
    """The configured model does not exist on the endpoint (404 / model not found)."""

    code = "ollama_unsupported_model"
    message_key = "error.model_unavailable"


# --------------------------------------------------------------------------- #
# Google Calendar (user OAuth)
# --------------------------------------------------------------------------- #
class GoogleError(AssistantError):
    """Base class for Google API failures behind the user's OAuth refresh token."""

    code = "google_error"
    message_key = "error.calendar_unavailable"


class GoogleAuthError(GoogleError):
    """The refresh token was rejected or revoked; the user must re-authorize."""

    code = "google_auth"
    message_key = "error.google_auth"


class GoogleTimeout(GoogleError):
    """The Calendar API did not answer in time. Ambiguous for writes."""

    code = "google_timeout"
    message_key = "error.timeout"


class GoogleUnavailable(GoogleError):
    """Transport failure or 5xx. Ambiguous for writes."""

    code = "google_unavailable"


class GoogleRateLimited(GoogleError):
    """429, or a 403 whose reason is a quota/rate limit. The write did not happen."""

    code = "google_rate_limited"
    message_key = "error.rate_limited"


class GoogleBadRequest(GoogleError):
    """A definite 4xx (bad event fields, unknown calendar, forbidden)."""

    code = "google_bad_request"
    message_key = "error.calendar_bad_request"


class GoogleConflict(GoogleError):
    """The supplied event id already exists (409); the caller reconciles by reading it."""

    code = "google_conflict"
    message_key = "error.calendar_conflict"


class GoogleBadResponse(GoogleError):
    """A 2xx whose body is not the documented shape. Ambiguous for writes."""

    code = "google_bad_response"


class CalendarEventNotFound(GoogleError):
    code = "calendar_event_not_found"
    message_key = "error.calendar_event_not_found"


class CalendarEventChanged(GoogleError):
    """The remote event no longer matches what the user approved."""

    code = "calendar_event_changed"
    message_key = "error.calendar_event_changed"


__all__ = [
    "AssistantError",
    "BudgetExceeded",
    "CalendarEventChanged",
    "CalendarEventNotFound",
    "CapabilityUnavailable",
    "ConfigError",
    "GoogleAuthError",
    "GoogleBadRequest",
    "GoogleBadResponse",
    "GoogleConflict",
    "GoogleError",
    "GoogleRateLimited",
    "GoogleTimeout",
    "GoogleUnavailable",
    "OllamaBadResponse",
    "OllamaError",
    "OllamaRateLimited",
    "OllamaTimeout",
    "OllamaUnavailable",
    "OllamaUnsupportedModel",
    "RateLimited",
    "TelegramApiError",
    "TelegramError",
    "TelegramRateLimited",
    "UnauthorisedUser",
    "WebhookSecretInvalid",
]
