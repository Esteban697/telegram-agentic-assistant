"""The durable resume queue (PLAN.md §5.5, §10 Phase 3).

Layer 3. The handler enqueues a **watchdog** row *before* entering the graph and marks it `done`
after the reply has been sent. That ordering is the whole crash-safety argument: if the process
dies anywhere inside the turn — including the SIGTERM drain Cloud Run performs — the row is
already queued, so the resume job finds the thread and finishes the turn instead of the user
waiting forever.

It is also the second-turn rule (RB1-follow-on): while a turn is in flight the row is `pending`,
so a second message's enqueue conflicts, the handler declines it explicitly, and no second graph
run starts on one thread. An index *rejects*; it does not serialise, so the refusal is a policy
this service implements, not something the database does for us.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.storage import repositories

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TurnClaim:
    """The result of trying to open a turn."""

    opened: bool
    is_busy: bool
    thread_id: str


class WorkQueue:
    """Watchdog enqueue, completion, and the resume claim."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        lease_seconds: int,
        budget_seconds: float = 45.0,
    ) -> None:
        self._sessions = sessions
        self._lease_seconds = lease_seconds
        # The watchdog is **not due** until the turn's own budget has elapsed. Enqueuing it with
        # `run_at = now` let a scheduled resume claim the in-flight row, re-enter the graph on the
        # same thread and send a duplicate reply — the second concurrent run that (h) and
        # RB1-follow-on say cannot happen (found by the Phase 3 review).
        self._budget = timedelta(seconds=budget_seconds)

    async def open_turn(
        self,
        *,
        conversation_id: Any,
        thread_id: str,
        chat_id: int,
        now: datetime | None = None,
    ) -> TurnClaim:
        """Enqueue the watchdog before the graph runs.

        Returns `is_busy=True` when a turn is already in flight for this thread — the caller must
        then decline the message rather than start a second graph run. The payload carries the chat
        id, because that is the only thing the resume job can use to deliver the reply.
        """

        moment = now or datetime.now(UTC)
        async with self._sessions() as session, session.begin():
            item_id = await repositories.enqueue_work_item(
                session,
                conversation_id=conversation_id,
                thread_id=thread_id,
                kind="resume_thread",
                run_at=moment + self._budget,
                payload={"chat_id": chat_id},
            )
        if item_id is None:
            _logger.info(
                "turn declined: another turn is in flight",
                extra={"thread_id": thread_id, "status": "busy"},
            )
            return TurnClaim(opened=False, is_busy=True, thread_id=thread_id)
        return TurnClaim(opened=True, is_busy=False, thread_id=thread_id)

    async def close_turn(self, *, thread_id: str, failed: bool = False) -> int:
        """Mark the watchdog `done` once the reply has been sent.

        Called from a `finally`, so an unexpected failure closes the row instead of leaving it
        `pending` forever — which would decline every later message in that chat as busy.
        """

        async with self._sessions() as session, session.begin():
            return await repositories.mark_work_item_done(
                session, thread_id=thread_id, kind="resume_thread", state="failed" if failed else "done"
            )

    async def claim_due(self, *, batch: int, executor: str) -> list[dict[str, Any]]:
        async with self._sessions() as session, session.begin():
            return await repositories.claim_due_work_items(
                session,
                batch=batch,
                executor=executor,
                lease_seconds=self._lease_seconds,
            )

    async def finish(self, *, item_id: int, state: str, error: str | None = None) -> int:
        async with self._sessions() as session, session.begin():
            return await repositories.finish_work_item(
                session, item_id=item_id, state=state, error=error
            )

    async def state_of(self, *, thread_id: str) -> str | None:
        async with self._sessions() as session:
            return await repositories.work_item_state(
                session, thread_id=thread_id, kind="resume_thread"
            )


__all__ = ["TurnClaim", "WorkQueue"]
