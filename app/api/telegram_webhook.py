"""The Telegram webhook (PLAN.md §7.1, §7.6, §10 Phase 1 deliverable 8).

Order of operations, and why it is this order:

1. **Secret** — an unsigned request is never parsed. Constant-time comparison over **bytes**;
   ``hmac.compare_digest`` raises ``TypeError`` on non-ASCII ``str``, which would turn a
   crafted header into a 500 instead of the documented 403 (N11), so the comparison is
   wrapped and the failure is a clean denial.
2. **Body cap** — rejected with 413 before JSON parsing, so a large body is not a DoS vector.
3. **Dedupe** — the first write in the handler, before anything else can happen, so a
   Telegram retry is harmless (PLAN.md §5.1). This is the property that makes scale-to-zero
   and the 45 s budget safe.
4. **Allow-list** — deny by default; an unauthorised update is recorded and answered 200 so
   Telegram stops retrying, and produces **no** model call and **no** reply.
5. **Rate limit** — after the dedupe insert, so a duplicated update consumes no budget
   (§7.6); over-limit still answers 200 with exactly one notice per window, never a silent
   drop.
6. **Handle** — the handler runs the turn; every outcome is recorded in the ledger, and the
   endpoint answers 200 in all cases so that a failure is not retried into a duplicate.

The per-user token bucket state lives here, per instance, as §7.6 specifies.
"""

from __future__ import annotations

import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.bot.handlers import TelegramHandlers
from app.bot.messages import render
from app.config import Settings
from app.errors import AssistantError, UnauthorisedUser
from app.logging_config import clear_request_context, set_request_context
from app.policy import limits
from app.policy.authz import TelegramIdentity
from app.storage.stores import (
    STATUS_FAILED,
    STATUS_IGNORED,
    STATUS_PROCESSED,
    DedupeStore,
)

_logger = logging.getLogger(__name__)

router = APIRouter()

SECRET_HEADER = "x-telegram-bot-api-secret-token"


@dataclass
class _UserLimit:
    """Per-user bucket plus the "one notice per window" flag (§7.6)."""

    state: limits.BucketState
    notice_sent: bool = False
    extras: dict[str, Any] = field(default_factory=dict)


class RateLimiter:
    """Per-instance, per-user token buckets.

    Honest limitation (Risks item 17): the effective limit is
    ``RATE_LIMIT_PER_MINUTE`` times the number of live instances, and a cold start resets a
    caller's budget. Acceptable for a single-user assistant; a distributed limiter would need
    shared state, and Redis is named explicitly by spec §6 as a component not to add.
    """

    def __init__(self, *, per_minute: int) -> None:
        self._capacity = per_minute
        self._rate = limits.refill_rate_for(per_minute)
        self._buckets: dict[int, _UserLimit] = {}

    def check(self, user_id: int, *, now: float | None = None) -> tuple[bool, bool]:
        """Return ``(allowed, should_notify)``.

        ``should_notify`` is True at most once per window, so an over-limit burst produces
        one message rather than one per request.
        """

        moment = time.monotonic() if now is None else now
        entry = self._buckets.get(user_id)
        if entry is None:
            entry = _UserLimit(state=limits.new_bucket(self._capacity, moment))
            self._buckets[user_id] = entry

        allowed, new_state = limits.consume(
            entry.state,
            capacity=self._capacity,
            refill_per_second=self._rate,
            now=moment,
        )
        entry.state = new_state
        if allowed:
            entry.notice_sent = False
            return True, False
        if entry.notice_sent:
            return False, False
        entry.notice_sent = True
        return False, True


@router.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> Response:
    """Receive one Telegram update."""

    settings: Settings = request.app.state.settings
    request_id = uuid.uuid4().hex
    set_request_context(request_id=request_id)
    try:
        return await _handle_request(request, settings, request_id=request_id)
    finally:
        clear_request_context()


async def _handle_request(request: Request, settings: Settings, *, request_id: str) -> Response:
    raw_secret = request.headers.get(SECRET_HEADER)
    if not _secret_matches(raw_secret, settings.webhook_secret):
        # 403 with an empty body; the submitted value is never logged.
        _logger.warning(
            "webhook rejected",
            extra={"error_code": "webhook_secret_invalid", "status": 403},
        )
        return Response(status_code=403)

    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            if int(declared_length) > settings.max_webhook_body_bytes:
                return Response(status_code=413)
        except ValueError:
            return Response(status_code=413)

    body = await request.body()
    if len(body) > settings.max_webhook_body_bytes:
        return Response(status_code=413)

    try:
        update = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        # A malformed update that passed the secret is recorded and dropped: returning an
        # error would make Telegram retry a body that can never parse.
        _logger.warning(
            "webhook body was not valid JSON",
            extra={"error_code": "webhook_bad_json", "status": 200},
        )
        return JSONResponse(status_code=200, content={"ok": True})

    if not isinstance(update, dict):
        _logger.warning(
            "webhook body was not a JSON object",
            extra={"error_code": "webhook_bad_json", "status": 200},
        )
        return JSONResponse(status_code=200, content={"ok": True})

    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        _logger.warning(
            "update without an integer update_id",
            extra={"error_code": "update_id_missing", "status": 200},
        )
        return JSONResponse(status_code=200, content={"ok": True})

    set_request_context(update_id=update_id)

    dedupe: DedupeStore = request.app.state.dedupe
    handlers: TelegramHandlers = request.app.state.handlers
    limiter: RateLimiter = request.app.state.rate_limiter
    kind = _kind_of(update)

    if not await dedupe.claim(update_id, kind=kind):
        _logger.info("duplicate update ignored", extra={"status": STATUS_IGNORED})
        return JSONResponse(status_code=200, content={"ok": True})

    from app.bot.auth import authorize  # local import: keeps the router surface small

    try:
        identity = authorize(update, settings)
    except UnauthorisedUser as exc:
        await dedupe.mark(update_id, status=STATUS_IGNORED, error_code=exc.code)
        _logger.warning("unauthorised update ignored", extra=exc.as_log_fields())
        return JSONResponse(status_code=200, content={"ok": True})

    set_request_context(conversation_id=str(identity.chat_id))

    allowed, should_notify = limiter.check(identity.user_id)
    if not allowed:
        await dedupe.mark(update_id, status=STATUS_IGNORED, error_code="rate_limited")
        _logger.warning("rate limited", extra={"error_code": "rate_limited", "status": 200})
        if should_notify:
            await _safe_reply(handlers, identity, render("error.rate_limited"))
        return JSONResponse(status_code=200, content={"ok": True})

    try:
        outcome = await handlers.handle(update, identity)
    except AssistantError as exc:
        await dedupe.mark(update_id, status=STATUS_FAILED, error_code=exc.code)
        _logger.error("handler failed", extra=exc.as_log_fields())
        await _safe_reply(handlers, identity, render(exc.message_key))
        return JSONResponse(status_code=200, content={"ok": True})
    except Exception as exc:  # pragma: no cover - defensive; must not 500 back to Telegram
        await dedupe.mark(update_id, status=STATUS_FAILED, error_code="unhandled_error")
        _logger.error(
            "unhandled handler error",
            extra={"error_code": "unhandled_error"},
            exc_info=exc,
        )
        await _safe_reply(handlers, identity, render("error.generic"))
        return JSONResponse(status_code=200, content={"ok": True})

    await dedupe.mark(update_id, status=outcome.status)
    _logger.info(
        "update handled",
        extra={"status": outcome.status, "detail": outcome.detail, "replied": outcome.replied},
    )
    return JSONResponse(status_code=200, content={"ok": True})


def _secret_matches(provided: str | None, expected: str) -> bool:
    """Constant-time comparison over bytes, failing closed on any unexpected input."""

    if not provided or not expected:
        return False
    try:
        return hmac.compare_digest(
            provided.encode("utf-8", "surrogateescape"),
            expected.encode("utf-8"),
        )
    except Exception:  # pragma: no cover - defensive: must be a clean 403, not a 500
        return False


async def _safe_reply(handlers: TelegramHandlers, identity: TelegramIdentity, text: str) -> None:
    """Best-effort error reply: a failure to notify must not change the HTTP outcome.

    Catches everything, not just :class:`~app.errors.AssistantError`: this runs on the error
    path, and the one thing that must never happen is a crash here turning into a 500 that
    makes Telegram retry the update that just failed.
    """

    try:
        await handlers.notify(identity.chat_id, text)
    except Exception as exc:  # noqa: BLE001 - deliberate catch-all on the error path
        _logger.warning(
            "could not deliver error notice",
            extra={"error_code": "notice_delivery_failed", "exc_type": type(exc).__name__},
        )


def _kind_of(update: dict[str, Any]) -> str:
    if "message" in update:
        return "message"
    if "edited_message" in update:
        return "edited_message"
    if "callback_query" in update:
        return "callback_query"
    return "unsupported"


__all__ = ["RateLimiter", "SECRET_HEADER", "router"]
