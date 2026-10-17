"""The approval state machine, as pure functions (PLAN.md §4.1, §4.5).

Layer 0 and **pure**: transitions, guards, the canonical argument hash, the callback-data
format, and the zero-row diagnosis. The database statements live in
:mod:`app.storage.repositories`; this module decides what they mean.

Keeping the machine here is what makes §9.1's exhaustive 8×8 transition test possible without a
database, and it is the single authority for which transitions are legal (§15).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

# The states, exactly the eight of spec §7. A `StrEnum` so the values are what the CHECK
# constraint holds, without a second mapping to drift from.
PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXPIRED = "expired"
EXECUTING = "executing"
SUCCEEDED = "succeeded"
FAILED = "failed"
UNKNOWN = "unknown"


class ActionState(StrEnum):
    PENDING = PENDING
    APPROVED = APPROVED
    REJECTED = REJECTED
    EXPIRED = EXPIRED
    EXECUTING = EXECUTING
    SUCCEEDED = SUCCEEDED
    FAILED = FAILED
    UNKNOWN = UNKNOWN


TERMINAL: frozenset[str] = frozenset({REJECTED, EXPIRED, SUCCEEDED, FAILED})

# (from, to) -> trigger. Anything absent is denied.
TRANSITIONS: dict[tuple[str, str], str] = {
    (PENDING, APPROVED): "user_approves",
    (PENDING, REJECTED): "user_rejects",
    (PENDING, EXPIRED): "ttl_elapsed",
    (APPROVED, EXECUTING): "claim",
    (APPROVED, EXPIRED): "ttl_elapsed",
    (EXECUTING, SUCCEEDED): "executor_result",
    (EXECUTING, FAILED): "executor_result",
    (EXECUTING, UNKNOWN): "executor_ambiguous",
    (UNKNOWN, SUCCEEDED): "reconciliation",
    (UNKNOWN, FAILED): "reconciliation",
}

# States from which a retry may create a NEW row (never a re-execution of the old one).
RETRYABLE: frozenset[str] = frozenset({FAILED, UNKNOWN})


class InvalidTransition(RuntimeError):
    code = "invalid_transition"

    def __init__(self, source: str, target: str) -> None:
        super().__init__(f"{source} -> {target} is not a legal approval transition")
        self.source = source
        self.target = target


def can_transition(source: str, target: str) -> bool:
    return (source, target) in TRANSITIONS


def assert_transition(source: str, target: str) -> None:
    if not can_transition(source, target):
        raise InvalidTransition(source, target)


def is_terminal(state: str) -> bool:
    return state in TERMINAL


def is_blocking(state: str) -> bool:
    """`unknown` blocks re-execution without being terminal (PLAN.md §4.1)."""

    return state == UNKNOWN


def can_retry(state: str) -> bool:
    return state in RETRYABLE


# --------------------------------------------------------------------------- #
# Canonical arguments and the hash
# --------------------------------------------------------------------------- #
def canonical_args(args: dict[str, Any]) -> str:
    """A deterministic serialisation, so the same arguments always hash the same.

    Sorted keys, no insignificant whitespace, and non-ASCII kept as-is. This is the
    canonicalisation the *application* owns; the trigger deliberately does not try to
    reproduce it in SQL (RN10).
    """

    return json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_args_hash(args: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_args(args).encode("utf-8")).hexdigest()


def numbers_as_floats(value: Any) -> Any:
    """Recursively render every number as a float.

    `jsonb` does not round-trip floats faithfully: `1e20` comes back as an `int`, `-0.0` as
    `0.0`. Comparing the stored arguments with a fresh canonicalisation therefore produced a
    *false* mismatch for legitimate float arguments (prices, coordinates, scores — all of which
    Phase 4 will have). Normalising both sides to floats keeps a genuine difference and drops the
    representation difference.
    """

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        return {key: numbers_as_floats(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [numbers_as_floats(item) for item in value]
    return value


def same_args(left: Any, right: Any) -> bool:
    """True when two argument structures are the same, ignoring int/float representation."""

    return canonical_args(numbers_as_floats(left)) == canonical_args(numbers_as_floats(right))


def hash_prefix(args_hash: str, length: int = 16) -> str:
    """The truncated hash carried in `callback_data` (48 bytes total, inside Telegram's 64)."""

    return args_hash[:length]


# --------------------------------------------------------------------------- #
# callback_data
# --------------------------------------------------------------------------- #
CALLBACK_PREFIX = "ap"


def build_callback_data(
    *, action_id: str, approve: bool, args_revision: int, args_hash: str
) -> str:
    """`ap:<action_id>:<a|r>:<args_revision>:<args_hash[:16]>` — 48 bytes (PLAN.md §4.1)."""

    decision = "a" if approve else "r"
    return f"{CALLBACK_PREFIX}:{action_id}:{decision}:{args_revision}:{hash_prefix(args_hash)}"


@dataclass(frozen=True, slots=True)
class ApprovalCallback:
    action_id: str
    approve: bool
    args_revision: int
    args_hash_prefix: str


def parse_callback_data(data: str) -> ApprovalCallback | None:
    """Parse an approval callback. Anything malformed is `None`, never a guess."""

    parts = data.split(":")
    if len(parts) != 5 or parts[0] != CALLBACK_PREFIX:
        return None
    _, action_id, decision, revision, prefix = parts
    if not action_id or decision not in {"a", "r"}:
        return None
    if not revision.isdigit():
        return None
    if len(prefix) != 16:
        return None
    return ApprovalCallback(
        action_id=action_id,
        approve=decision == "a",
        args_revision=int(revision),
        args_hash_prefix=prefix,
    )


# --------------------------------------------------------------------------- #
# The zero-row diagnosis (§4.5)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class RowView:
    """The columns the diagnostic read selects. `None` when there is no row."""

    state: str | None
    args_revision: int | None
    args_hash: str | None
    expires_at: datetime | None
    user_id: int | None
    chat_id: int | None
    decided_by: int | None


def diagnose_zero_rows(
    row: RowView | None,
    *,
    callback: ApprovalCallback,
    user_id: int,
    chat_id: int,
    now: datetime,
) -> str:
    """Return the message key for the row's **actual** cause (never an asserted one).

    One key per cause, seven in total (§4.5, RB2): the merged "another caller holds, or has just
    taken, the claim" row is one entry with one key.
    """

    if row is None or row.state is None:
        return "approval.unknown_action"

    if row.user_id != user_id or row.chat_id != chat_id:
        # Deliberately vague to the user; the log carries the precise reason.
        return "approval.wrong_identity"

    # The revision/hash comparison comes **before** the state-based branches (round-2 review of
    # Phase 3): otherwise a tampered callback against an *approved* row was reported as "already
    # in progress", and §4.5's row 6 was unreachable. A callback whose revision or hash prefix
    # does not match the row is talking about a different revision, whatever the state is.
    if row.state in {PENDING, APPROVED}:  # terminal states cannot have a stale button
        if row.args_revision != callback.args_revision:
            return "approval.arguments_changed"
        if (row.args_hash or "")[: len(callback.args_hash_prefix)] != callback.args_hash_prefix:
            return "approval.arguments_changed"

    if row.expires_at is not None and row.expires_at <= now:
        return "approval.expired"

    if row.state == REJECTED:
        return "approval.already_rejected"

    if row.state in {EXECUTING, APPROVED}:
        # `approved` here means the claim matched nothing while another caller holds the row
        # lock; both conditions share one cause and therefore one key.
        return "approval.in_progress"

    if row.state in {SUCCEEDED, FAILED, UNKNOWN}:
        return "approval.already_resolved"

    # A row that is still `pending` and matched every guard means the decision CAS lost a race
    # with another caller: report that rather than inventing a cause.
    return "approval.in_progress"


ZERO_ROW_KEYS: tuple[str, ...] = (
    "approval.unknown_action",
    "approval.wrong_identity",
    "approval.expired",
    "approval.already_rejected",
    "approval.in_progress",
    "approval.already_resolved",
    "approval.arguments_changed",
)


__all__ = [
    "numbers_as_floats",
    "same_args",
    "APPROVED",
    "CALLBACK_PREFIX",
    "EXECUTING",
    "EXPIRED",
    "FAILED",
    "PENDING",
    "REJECTED",
    "RETRYABLE",
    "SUCCEEDED",
    "TERMINAL",
    "TRANSITIONS",
    "UNKNOWN",
    "ZERO_ROW_KEYS",
    "ActionState",
    "ApprovalCallback",
    "InvalidTransition",
    "RowView",
    "assert_transition",
    "build_callback_data",
    "can_retry",
    "can_transition",
    "canonical_args",
    "compute_args_hash",
    "diagnose_zero_rows",
    "hash_prefix",
    "is_blocking",
    "is_terminal",
    "parse_callback_data",
]
