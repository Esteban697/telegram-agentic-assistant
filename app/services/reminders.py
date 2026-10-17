"""The reminder dispatcher (PLAN.md §5.2 — reconcile, claim, ledger, advance).

Layer 3. It owns the *logic*; the timer is Cloud Scheduler driving `app/jobs/dispatch_reminders.py`
(spec §11: no `asyncio.sleep`, no in-memory timer, no long-running process).

Every mutating step is a single guarded statement at `READ COMMITTED` (§4.6), and the three
phases run in this order for a reason:

**Phase 0 — reconcile.** A `claimed` ledger row whose lease expired becomes `unknown` and is
reported. This closes the crash window between the ledger insert and `sendMessage`: such a row
used to stay `claimed` forever, the unique constraint blocked any later insert for that
occurrence, and the reminder vanished without being reported as anything (B2). Nothing is ever
auto-resent — Telegram has no idempotency key.

**Phase 1 — claim.** `FOR UPDATE SKIP LOCKED` gives concurrent dispatchers disjoint batches,
and `attempts < max_attempts` is enforced in SQL so an exhausted task cannot be picked up (B1).
`attempts` is *not* incremented here: an attempt is consumed only when the ledger insert
returned a row, so contention cannot burn a task's retries (RN6).

**Phase 2 — deliver.** The ledger insert is the right to send; a returned row means we own the
occurrence, no row means somebody else already handled it. On success the ledger mark and the
task advance happen in **the same transaction** (B1), so there is no window in which the send is
recorded but the task still looks due. A zero-row ledger CAS means another run reconciled the
occurrence while we were sending: the message did go out, so the task is still advanced and the
discrepancy is logged (RN5).
"""

from __future__ import annotations

import logging
import zoneinfo
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.errors import AssistantError
from app.services.notifications import Notifier
from app.storage import repositories

_logger = logging.getLogger(__name__)

# Bounded backoff between delivery attempts (spec §11 "reintentos acotados").
RETRY_BASE_SECONDS = 60
RETRY_CAP_SECONDS = 3600


def retry_delay_seconds(attempts: int) -> int:
    """Delay before the next attempt: 60s, 120s, 240s … capped at an hour."""

    exponent = max(0, attempts - 1)
    return min(RETRY_BASE_SECONDS * (2**exponent), RETRY_CAP_SECONDS)


@dataclass
class DispatchReport:
    """What one dispatcher run did. Printed as JSON by the job, for the operator and the tests."""

    reconciled: list[tuple[str, str]] = field(default_factory=list)
    claimed: int = 0
    sent: list[dict[str, object]] = field(default_factory=list)
    skipped: list[dict[str, object]] = field(default_factory=list)
    failed: list[dict[str, object]] = field(default_factory=list)
    discrepancies: list[dict[str, object]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "reconciled": len(self.reconciled),
            "reconciled_items": [{"task_id": t, "occurrence_at": o} for t, o in self.reconciled],
            "claimed": self.claimed,
            "sent": self.sent,
            "skipped": self.skipped,
            "failed": self.failed,
            "discrepancies": self.discrepancies,
        }


class ReminderDispatcher:
    """One-shot dispatcher run."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        notifier: Notifier,
        executor: str,
        batch_size: int,
        lease_seconds: int,
        max_attempts: int = 5,
        text_renderer: Callable[[Any], str] | None = None,
    ) -> None:
        self._sessions = sessions
        self._notifier = notifier
        self._executor = executor
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        # Injected by the job, which sits above the transport layer: a service may not import
        # `app.bot.messages` (§2.2), and the dispatcher must not invent user-facing text.
        self._render = text_renderer or default_text_renderer

    async def run_once(self, *, now: datetime | None = None) -> DispatchReport:
        report = DispatchReport()
        moment = now or datetime.now(UTC)

        await self._reconcile(report)
        claimed = await self._claim(report)

        for task in claimed:
            await self._deliver(task, report, now=moment)

        _logger.info("dispatch run complete", extra={"status": "ok", **{
            "sent": len(report.sent), "skipped": len(report.skipped), "failed": len(report.failed)
        }})
        return report

    # ------------------------------------------------------------------ #
    # Phase 0
    # ------------------------------------------------------------------ #
    async def _reconcile(self, report: DispatchReport) -> None:
        async with self._sessions() as session, session.begin():
            stale = await repositories.reconcile_stale_dispatches(
                session, lease_seconds=self._lease_seconds, executor=self._executor
            )
        for task_id, occurrence_at in stale:
            report.reconciled.append((str(task_id), occurrence_at.isoformat()))
            _logger.warning(
                "reconciled a stale reminder claim",
                extra={
                    "task_id": str(task_id),
                    "occurrence_at": occurrence_at.isoformat(),
                    "error_code": "dispatch_lease_expired",
                },
            )

    # ------------------------------------------------------------------ #
    # Phase 1
    # ------------------------------------------------------------------ #
    async def _claim(self, report: DispatchReport):  # noqa: ANN202 - ClaimedTask list
        async with self._sessions() as session, session.begin():
            claimed = await repositories.claim_due_tasks(
                session, batch=self._batch_size, executor=self._executor
            )
        report.claimed = len(claimed)
        return claimed

    # ------------------------------------------------------------------ #
    # Phase 2
    # ------------------------------------------------------------------ #
    async def _deliver(self, task, report: DispatchReport, *, now: datetime) -> None:  # noqa: ANN001
        occurrence = task.occurrence_at or task.due_at
        dispatch_key = f"{task.task_id}:{occurrence.isoformat()}"

        async with self._sessions() as session, session.begin():
            dispatch_id = await repositories.claim_dispatch(
                session,
                task_id=task.task_id,
                occurrence_at=occurrence,
                dispatch_key=dispatch_key,
                executor=self._executor,
            )
            if dispatch_id is None:
                # Another run already owns this occurrence: consume no attempt (RN6).
                report.skipped.append(
                    {"task_id": str(task.task_id), "reason": "already_claimed"}
                )
                _logger.info(
                    "occurrence already claimed elsewhere",
                    extra={"task_id": str(task.task_id), "status": "skipped"},
                )
                return
            await repositories.increment_task_attempts(session, task_id=task.task_id)

        text = self._render(task)
        result = await self._notifier.send(chat_id=task.user_id, text=text)

        if result.delivered:
            await self._mark_success(task, dispatch_id, result, report, now=now)
        else:
            await self._mark_failure(task, dispatch_id, result, report, now=now)

    async def _mark_success(self, task, dispatch_id, result, report, *, now: datetime) -> None:  # noqa: ANN001
        async with self._sessions() as session, session.begin():
            marked = await repositories.mark_dispatch_sent(
                session, dispatch_id=dispatch_id, telegram_message_id=result.message_id
            )
            if marked == 0:
                # Phase 0 reconciled the row while we were sending. The message went out, so
                # the task is still advanced; the discrepancy is reported rather than hidden.
                report.discrepancies.append(
                    {
                        "task_id": str(task.task_id),
                        "dispatch_id": dispatch_id,
                        "reason": "ledger_row_no_longer_claimed",
                    }
                )
                _logger.warning(
                    "ledger discrepancy on delivery",
                    extra={
                        "task_id": str(task.task_id),
                        "dispatch_id": dispatch_id,
                        "error_code": "ledger_discrepancy",
                    },
                )
            await repositories.advance_task_on_delivery(session, task_id=task.task_id)

        report.sent.append(
            {
                "task_id": str(task.task_id),
                "message_id": result.message_id,
                "occurrence_at": (task.occurrence_at or task.due_at).isoformat(),
            }
        )
        _logger.info(
            "reminder delivered",
            extra={"task_id": str(task.task_id), "status": "sent"},
        )

    async def _mark_failure(self, task, dispatch_id, result, report, *, now: datetime) -> None:  # noqa: ANN001
        retry_at = now + timedelta(seconds=retry_delay_seconds(max(1, task.attempts + 1)))
        async with self._sessions() as session, session.begin():
            await repositories.mark_dispatch_failed(
                session, dispatch_id=dispatch_id, error=str(result.error)
            )
            state = await repositories.advance_task_on_failure(
                session,
                task_id=task.task_id,
                error=str(result.error),
                retry_at=retry_at,
            )

        report.failed.append(
            {"task_id": str(task.task_id), "state": state, "error": result.error}
        )
        _logger.warning(
            "reminder delivery failed",
            extra={
                "task_id": str(task.task_id),
                "status": "failed",
                "error_code": result.error,
                "next_state": state,
            },
        )


def default_text_renderer(task: Any) -> str:
    """A neutral fallback used when no renderer is injected (tests, and defensive wiring).

    Production injects `messages.render('reminder.due', ...)` from the job, because the
    Spanish catalogue lives in `app/bot` and this layer may not import it.
    """

    return f"{task.title} ({task.due_at.isoformat()})"


__all__ = [
    "RETRY_BASE_SECONDS",
    "RETRY_CAP_SECONDS",
    "DispatchReport",
    "ReminderDispatcher",
    "retry_delay_seconds",
]
