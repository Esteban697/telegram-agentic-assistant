"""Execution limits: the token bucket behind ``RATE_LIMIT_PER_MINUTE`` (PLAN.md §7.6).

Layer 0 and **pure**: no clock is read here, no I/O, no imports from other ``app`` packages.
Time is a parameter, so the algorithm is unit-testable with zero credentials and no
``sleep`` — and per PLAN.md §11 the production reminder mechanism is a database scan driven
by Cloud Scheduler, never a timer, so nothing in this module schedules anything.

Honest limitation, documented in the plan (Risks item 17): the bucket state lives per
process, so the effective limit is ``RATE_LIMIT_PER_MINUTE`` times the number of live
instances, and a cold start resets a caller's budget. That is deliberate for a single-user
assistant and is why this module holds no shared state.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BucketState:
    """Immutable bucket state: tokens available and when it was last updated."""

    tokens: float
    updated_at: float


def new_bucket(capacity: int, now: float) -> BucketState:
    """Return a full bucket. ``capacity`` must be positive."""

    if capacity <= 0:
        raise ValueError("capacity must be positive")
    return BucketState(tokens=float(capacity), updated_at=now)


def refill(
    state: BucketState,
    *,
    capacity: int,
    refill_per_second: float,
    now: float,
) -> BucketState:
    """Return the state with tokens accrued since ``state.updated_at``."""

    elapsed = max(0.0, now - state.updated_at)
    tokens = min(float(capacity), state.tokens + elapsed * refill_per_second)
    return BucketState(tokens=tokens, updated_at=now)


def consume(
    state: BucketState,
    *,
    capacity: int,
    refill_per_second: float,
    now: float,
    cost: float = 1.0,
) -> tuple[bool, BucketState]:
    """Try to spend ``cost`` tokens.

    Returns ``(allowed, new_state)``. A denied request still returns a state with the
    elapsed tokens accrued, so repeatedly hammering the endpoint does not reset the clock.
    """

    if cost <= 0:
        raise ValueError("cost must be positive")

    accrued = refill(state, capacity=capacity, refill_per_second=refill_per_second, now=now)
    if accrued.tokens >= cost:
        return True, BucketState(tokens=accrued.tokens - cost, updated_at=accrued.updated_at)
    return False, accrued


def refill_rate_for(per_minute: int) -> float:
    """Tokens per second for a per-minute limit."""

    if per_minute <= 0:
        raise ValueError("per_minute must be positive")
    return per_minute / 60.0


__all__ = ["BucketState", "consume", "new_bucket", "refill", "refill_rate_for"]
