"""Approvals: create, decide, claim, execute (PLAN.md §4.2, §4.5, §7).

Layer 3. Every guard is in the SQL, so the mutual exclusion is the row lock rather than
application logic; this module decides what the outcomes *mean*.

Two things here close findings from earlier reviews:

* **zero rows is diagnosed, never asserted** — the message comes from the row's actual state
  (§4.5), so a user whose approval merely expired is not told an action is in progress;
* **the argument hash is re-verified in the application transaction before execution.** The
  `pending_actions_args_immutable` trigger permits a row to be pushed back to `pending` with a
  bumped `args_revision` *and a stale `args_hash`* (RN10: SQL cannot recompute the canonical
  hash), so the database alone cannot promise that the approved arguments are the ones that run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

# Module level, deliberately: `prepare` catches `sa.exc.IntegrityError` on the duplicate-action
# path, and that fix shipped with only a *function-local* import (inside `_find_live_sql`), so
# the handler raised NameError instead of handling the conflict.
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.errors import AssistantError
from app.policy import approval as machine
from app.policy.approval import ActionState, ApprovalCallback
from app.storage import repositories

_logger = logging.getLogger(__name__)


class ArgumentsChanged(AssistantError):
    """The stored arguments no longer match the stored hash: refuse to execute.

    This is the application-side half of §3.3's "approved arguments cannot be mutated". The
    database trigger stops a plain rewrite, but it cannot recompute a canonical JSON hash, so a
    row could be re-presented with a mismatched hash. Checking here turns that into a refusal.
    """

    code = "approval_args_hash_mismatch"
    message_key = "approval.arguments_changed"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """**Frozen contract.** What the graph prepares and the bot renders.

    `args_hash` and `args_revision` are what the button carries and the claim compares; nothing
    in the callback payload is trusted beyond the action id, the decision, the revision and the
    16-character hash prefix.
    """

    action_id: str
    action_type: str
    args: dict[str, Any]
    args_hash: str
    args_revision: int
    preview_text: str
    idempotency_key: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Decision:
    """The outcome of a decision or a claim, with the cause when it did not apply."""

    applied: bool
    state: str | None = None
    message_key: str = "approval.in_progress"
    action_id: str | None = None


@dataclass(frozen=True, slots=True)
class Execution:
    """What the executor should do, and what happened."""

    claimed: bool
    message_key: str
    action_id: str | None = None
    action_type: str | None = None
    args: dict[str, Any] | None = None
    idempotency_key: str | None = None
    cause: str | None = None


class ApprovalService:
    """Approval lifecycle against PostgreSQL."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        ttl_minutes: int,
    ) -> None:
        self._sessions = sessions
        self._ttl = timedelta(minutes=ttl_minutes)

    # ------------------------------------------------------------------ #
    # Creation (the graph prepares, nothing runs)
    # ------------------------------------------------------------------ #
    async def prepare(
        self,
        *,
        user_id: int,
        chat_id: int,
        action_type: str,
        args: dict[str, Any],
        preview_text: str,
        now: datetime | None = None,
        expires_in: timedelta | None = None,
        validator: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> ApprovalRequest:
        """Create a `pending` action. The same logical action is never stacked twice."""

        moment = now or datetime.now(UTC)
        # Validate here as well as at execution: every tool has an argument schema (spec §10), and
        # storing an unvalidated payload showed the user an approval prompt for a call that would
        # be rejected at execution time.
        # The caller supplies the validator, because `app.tools` sits above this layer (§2.2):
        # the graph passes the tool's own argument model. Without one, the payload is stored as
        # given — which only happens in tests that build the service directly.
        if validator is not None:
            args = validator(args)

        canonical = machine.canonical_args(args)
        args_hash = machine.compute_args_hash(args)
        action_id = secrets.token_urlsafe(16)
        base_key = f"{action_type}:{user_id}:{args_hash[:32]}"

        async with self._sessions() as session:
            # `idempotency_key` is UNIQUE (spec §3.2) and a *terminal* row keeps its key, so a
            # re-request of the same action must be a distinct row: the attempt number is folded
            # into the key (§4.1 "a retry is a new row"). The partial live index remains the
            # arbiter that stops the same action being *stacked* while it is still live.
            async with session.begin():
                attempt = (
                    await repositories.count_actions_for_key_prefix(
                        session, idempotency_prefix=base_key
                    )
                    + 1
                )
            idempotency_key = f"{base_key}#{attempt}"
            expires_at = moment + (expires_in or self._ttl)
            try:
                async with session.begin():
                    inserted = await repositories.insert_action(
                        session,
                        action_id=action_id,
                        user_id=user_id,
                        chat_id=chat_id,
                        action_type=action_type,
                        args_json=args,
                        args_hash=args_hash,
                        args_revision=1,
                        idempotency_key=idempotency_key,
                        preview_text=preview_text,
                        expires_at=expires_at,
                    )
            except sa.exc.IntegrityError:
                # A concurrent caller created the live action first: that is a normal outcome,
                # not an error (the reviewer reproduced an unhandled IntegrityError here).
                inserted = None

            if inserted is None:
                # An identical live action exists: re-use the row rather than adding a second
                # prompt (the partial unique index is the arbiter).
                async with session.begin():
                    rows = await session.execute(
                        _find_live_sql(), _find_live_params(user_id, action_type, args_hash)
                    )
                    row = rows.mappings().first()
                if row is None:  # pragma: no cover - the conflict proves it exists
                    raise ArgumentsChanged(
                        "the action could not be created and no live row was found",
                        context={"action_type": action_type},
                    )
                action_id = row["action_id"]
                expires_at = row["expires_at"]
                idempotency_key = row["idempotency_key"]

        return ApprovalRequest(
            action_id=action_id,
            action_type=action_type,
            args=args,
            args_hash=args_hash,
            args_revision=1,
            preview_text=preview_text,
            idempotency_key=idempotency_key,
            expires_at=expires_at,
        )

    async def attach_message(self, *, action_id: str, message_id: int) -> None:
        async with self._sessions() as session, session.begin():
            await repositories.set_action_message_id(
                session, action_id=action_id, message_id=message_id
            )

    # ------------------------------------------------------------------ #
    # Decision (step 1)
    # ------------------------------------------------------------------ #
    async def decide(
        self,
        *,
        callback: ApprovalCallback,
        user_id: int,
        chat_id: int,
    ) -> Decision:
        """The guarded decision CAS. Zero rows triggers the §4.5 diagnosis."""

        async with self._sessions() as session, session.begin():
            decided = await repositories.decide_action(
                session,
                action_id=callback.action_id,
                user_id=user_id,
                chat_id=chat_id,
                args_revision=callback.args_revision,
                approve=callback.approve,
            )

        if decided is not None:
            state = ActionState.APPROVED if callback.approve else ActionState.REJECTED
            machine.assert_transition(machine.PENDING, str(state))
            return Decision(applied=True, state=str(state), action_id=callback.action_id)

        cause = await self._diagnose(callback=callback, user_id=user_id, chat_id=chat_id)
        return Decision(applied=False, message_key=cause, action_id=callback.action_id)

    # ------------------------------------------------------------------ #
    # Claim (step 2) and execution
    # ------------------------------------------------------------------ #
    async def claim_and_execute(
        self,
        *,
        callback: ApprovalCallback,
        user_id: int,
        chat_id: int,
        executor: str,
    ) -> Execution:
        """Consume the approval atomically, then hand the executor what to run.

        The claim is one statement; zero rows is diagnosed from the row rather than assumed.
        Before returning, the stored arguments are re-hashed and compared with the stored hash
        (the stale-hash window), because the database trigger cannot do that recomputation.
        """

        async with self._sessions() as session, session.begin():
            claimed = await repositories.claim_action(
                session,
                action_id=callback.action_id,
                user_id=user_id,
                chat_id=chat_id,
                args_revision=callback.args_revision,
                args_hash_prefix=callback.args_hash_prefix,
                executor=executor,
            )

        if claimed is None:
            cause = await self._diagnose(callback=callback, user_id=user_id, chat_id=chat_id)
            _logger.info(
                "approval claim refused",
                extra={
                    "action_id": callback.action_id,
                    "status": "not_claimed",
                    "error_code": cause,
                },
            )
            return Execution(
                claimed=False, message_key=cause, action_id=callback.action_id, cause=cause
            )

        # --- the stale-hash window ------------------------------------------------------- #
        # The canonical text is what was hashed at creation, so comparing a *re-canonicalisation
        # of it* against the stored hash is float-safe (jsonb is not: `1e20` returns as an int).
        # The second comparison catches a re-presentation that rewrote `args_json` behind a
        # canonical text and hash that were left alone.
        stored_hash = hashlib.sha256(claimed.args_canonical.encode("utf-8")).hexdigest()
        args_match = machine.same_args(
            json.loads(claimed.args_canonical or "{}"), claimed.args_json or {}
        )
        if stored_hash != claimed.args_hash or not args_match:
            async with self._sessions() as session, session.begin():
                await repositories.finish_action(
                    session,
                    action_id=claimed.action_id,
                    state=ActionState.FAILED,
                    error="args_hash does not match args_json",
                    result_json={"reason": "args_hash_mismatch"},
                )
            _logger.error(
                "approved arguments do not match their stored hash",
                extra={
                    "action_id": claimed.action_id,
                    "status": "refused",
                    "error_code": "approval_args_hash_mismatch",
                },
            )
            raise ArgumentsChanged(
                "the stored args_json does not hash to the stored args_hash",
                context={"action_id": claimed.action_id},
            )

        return Execution(
            claimed=True,
            message_key="approval.executing",
            action_id=claimed.action_id,
            action_type=claimed.action_type,
            args=claimed.args_json or {},
            idempotency_key=claimed.idempotency_key,
        )

    async def finish(
        self,
        *,
        action_id: str,
        state: ActionState,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> int:
        """CAS `executing -> succeeded|failed|unknown`, so a stale executor cannot overwrite."""

        # The transition table is enforced here as well as in SQL, so an illegal state change
        # is a loud error rather than a silent `UPDATE` (the Phase 3 review found the table was
        # dead code and that a second claim could reach `attempts = 2`).
        machine.assert_transition(machine.EXECUTING, str(state))
        async with self._sessions() as session, session.begin():
            return await repositories.finish_action(
                session, action_id=action_id, state=str(state), result_json=result, error=error
            )

    # ------------------------------------------------------------------ #
    # Diagnosis
    # ------------------------------------------------------------------ #
    async def _diagnose(
        self, *, callback: ApprovalCallback, user_id: int, chat_id: int
    ) -> str:
        """Read the row *after* the zero-row result and report its actual cause."""

        async with self._sessions() as session:
            row = await repositories.diagnose_action(session, action_id=callback.action_id)

        key = machine.diagnose_zero_rows(
            row,
            callback=callback,
            user_id=user_id,
            chat_id=chat_id,
            now=datetime.now(UTC),
        )
        if key == "approval.wrong_identity":
            _logger.warning(
                "approval callback from the wrong identity",
                extra={
                    "action_id": callback.action_id,
                    "error_code": "approval_wrong_identity",
                    "status": "refused",
                },
            )
        return key


def _find_live_sql():  # noqa: ANN202 - small helper, kept next to its use
    import sqlalchemy as sa

    return sa.text(
        """
        SELECT action_id, idempotency_key, expires_at FROM pending_actions
         WHERE user_id = :user_id AND action_type = :action_type AND args_hash = :args_hash
           AND state IN ('pending', 'approved', 'executing')
         LIMIT 1
        """
    )


def _find_live_params(user_id: int, action_type: str, args_hash: str):  # noqa: ANN202
    return {"user_id": user_id, "action_type": action_type, "args_hash": args_hash}


__all__ = [
    "ApprovalRequest",
    "ApprovalService",
    "ArgumentsChanged",
    "Decision",
    "Execution",
]
