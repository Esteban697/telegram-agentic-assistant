"""Structured JSON logging with redaction (PLAN.md §8.2, §7.4).

Layer 0: stdlib only.

Two rules are enforced mechanically rather than by convention:

* **One JSON object per line**, with a fixed key set, so Cloud Logging can index it.
* **Never log secrets, message bodies, document text or embeddings.** The blocklist below is
  applied to every ``extra`` key an emitter passes; ``LOG_CONTENT_DEBUG=true`` is the single,
  documented opt-in that lifts it for content keys only.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

# Keys that must never reach a log record, wherever they appear.
_BLOCKED_KEYS: frozenset[str] = frozenset(
    {
        "token",
        "bot_token",
        "api_key",
        "apikey",
        "secret",
        "webhook_secret",
        "authorization",
        "headers",
        "password",
        "embedding",
        "embeddings",
        "vector",
        "content",
        "text",
        "message_text",
        "document_text",
        "payload",
        "body",
    }
)

# Content keys that become visible only when LOG_CONTENT_DEBUG is on.
_CONTENT_KEYS: frozenset[str] = frozenset({"content", "text", "message_text", "document_text"})

_REDACTED = "[redacted]"

# Request-scoped context, populated by the webhook handler so nested calls inherit it
# without threading parameters through every function (§8.2).
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_update_id: ContextVar[int | None] = ContextVar("update_id", default=None)
_conversation_id: ContextVar[str | None] = ContextVar("conversation_id", default=None)

_RESERVED_ATTRS: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def set_request_context(
    *,
    request_id: str | None = None,
    update_id: int | None = None,
    conversation_id: str | None = None,
) -> None:
    """Populate the per-request log context."""

    if request_id is not None:
        _request_id.set(request_id)
    if update_id is not None:
        _update_id.set(update_id)
    if conversation_id is not None:
        _conversation_id.set(conversation_id)


def clear_request_context() -> None:
    """Reset the per-request log context. Called in a ``finally`` block."""

    _request_id.set(None)
    _update_id.set(None)
    _conversation_id.set(None)


def redact(value: Any, *, key: str | None = None, content_debug: bool = False) -> Any:
    """Redact a value that is known to carry sensitive content.

    Redaction is **recursive**: a blocked key anywhere in the structure suppresses its value,
    including nested inside an unblocked container. The earlier form only looked at top-level
    keys, so ``extra={"meta": {"content": "..."}}`` carried a user message straight into the
    log (found by the Phase 1 review). Depth and breadth are bounded so a pathological
    structure cannot turn logging into a denial of service.
    """

    return _redact(value, key=key, content_debug=content_debug, depth=0)


_MAX_REDACT_DEPTH = 5
_MAX_REDACT_ITEMS = 50


def _redact(value: Any, *, key: str | None, content_debug: bool, depth: int) -> Any:
    if key is not None:
        lowered = key.lower()
        if lowered in _BLOCKED_KEYS:
            if content_debug and lowered in _CONTENT_KEYS:
                return _truncate(str(value))
            return _REDACTED

    if depth >= _MAX_REDACT_DEPTH:
        return "[truncated]"

    if isinstance(value, dict):
        return {
            item_key: _redact(
                item_value, key=str(item_key), content_debug=content_debug, depth=depth + 1
            )
            for item_key, item_value in list(value.items())[:_MAX_REDACT_ITEMS]
        }
    if isinstance(value, (list, tuple)):
        return [
            _redact(item, key=key, content_debug=content_debug, depth=depth + 1)
            for item in list(value)[:_MAX_REDACT_ITEMS]
        ]
    return value


def _truncate(value: str, limit: int = 200) -> str:
    return value if len(value) <= limit else value[:limit] + f"...[{len(value)} chars]"


# Shapes that must never survive into a log line, wherever they appear: bearer tokens, common
# API-key prefixes, long hex digests and long base64-ish runs. A pattern list is a weaker
# guarantee than key-based redaction, so it is applied *in addition to* it: `redact()` still
# blocks whole keys, and this scrubs values that carry a credential inside a free-text message.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"\b(sk|pk|rk|ghp|gho|xox[baprs])[-_][A-Za-z0-9_\-]{12,}"),
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
    # A long *mixed* run (upper, lower and digits) is base64-shaped; a long run of a single
    # character class is usually unbroken prose, so it is left to the truncation below instead
    # of being nuked. `re` has no easy entropy test, so the lookaheads do it.
    re.compile(
        r"\b(?=[A-Za-z0-9+/]*[A-Z])(?=[A-Za-z0-9+/]*[a-z])(?=[A-Za-z0-9+/]*[0-9])"
        r"[A-Za-z0-9+/]{40,}={0,2}\b"
    ),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_\-]{30,}\b"),
)


def scrub_text(value: str, *, limit: int = 300) -> str:
    """Keep a free-text diagnostic, minus anything shaped like a credential.

    Truncated as well, so a provider's echoed payload cannot become a log flood.
    """

    scrubbed = value.replace("\n", " ")
    for pattern in _SECRET_PATTERNS:
        scrubbed = pattern.sub(_REDACTED, scrubbed)
    return _truncate(scrubbed, limit)


def _clean_extras(record: logging.LogRecord, *, content_debug: bool) -> dict[str, Any]:
    extras: dict[str, Any] = {}
    for key, value in record.__dict__.items():
        if key in _RESERVED_ATTRS or key.startswith("_"):
            continue
        extras[key] = redact(value, key=key, content_debug=content_debug)
    return extras


class JsonFormatter(logging.Formatter):
    """Render one log record as a single-line JSON object."""

    def __init__(self, *, app_env: str, content_debug: bool = False) -> None:
        super().__init__()
        self._app_env = app_env
        self._content_debug = content_debug

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "app_env": self._app_env,
        }

        request_id = _request_id.get()
        update_id = _update_id.get()
        conversation_id = _conversation_id.get()
        if request_id is not None:
            payload["request_id"] = request_id
        if update_id is not None:
            payload["update_id"] = update_id
        if conversation_id is not None:
            payload["conversation_id"] = conversation_id

        payload.update(_clean_extras(record, content_debug=self._content_debug))

        if record.exc_info:
            # The traceback is useful but must never contain a secret: it is a stack
            # frame dump, not a payload, so it is kept while payload keys are stripped.
            payload["exc_type"] = getattr(record.exc_info[0], "__name__", "Exception")
            # NOT `redact(..., key="content")`: that replaced every exception message with
            # "[redacted]", which is why a bare `KeyError` had no message to diagnose with.
            # Secrets are still scrubbed, by shape rather than by key.
            payload["exc_message"] = scrub_text(str(record.exc_info[1]))
        return json.dumps(payload, ensure_ascii=False, default=str)


_HANDLER_MARKER = "_assistant_json_handler"


def configure_logging(*, level: str, app_env: str, content_debug: bool = False) -> None:
    """Install the JSON formatter on the root logger. Idempotent.

    Only handlers **this function installed** are replaced. An earlier revision removed every
    root handler, which silently detached a host's own handler — including pytest's log
    capture, which made the leak tests pass without capturing anything. A logging setup that
    disables its host's capture is also a hazard in production, where an embedding process may
    have attached its own handler.
    """

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter(app_env=app_env, content_debug=content_debug))
    setattr(handler, _HANDLER_MARKER, True)

    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, _HANDLER_MARKER, False):
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn installs its own handlers; route them through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True

    # httpx logs every request at INFO as "HTTP Request: POST <url> ...", and the Telegram
    # Bot API carries the bot token *in the URL path* — so httpx's own line would print a
    # credential (PLAN.md §7.4: tokens are never logged). Raising the level to WARNING keeps
    # transport failures visible while suppressing the URL line; our own log records carry
    # the method and the error code instead. The same applies to httpcore's connection logs,
    # which can echo the host.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


__all__ = [
    "JsonFormatter",
    "clear_request_context",
    "configure_logging",
    "redact",
    "scrub_text",
    "set_request_context",
]
