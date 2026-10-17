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


# --------------------------------------------------------------------------- #
# Retry backoff
#
# The schedule lives here, at layer 0, because two layers need it and only one may import
# the other: the Ollama client (layer 1) sleeps it, and configuration validation (layer 0)
# must account for it when checking that the provider timeout is reachable inside the
# request budget. Defining it in the client and importing it from config would invert the
# layering rule of PLAN.md §2.2.
# --------------------------------------------------------------------------- #
RETRY_BACKOFF_BASE_SECONDS = 0.25
RETRY_BACKOFF_CAP_SECONDS = 2.0


def backoff_seconds(attempt: int) -> float:
    """Delay before retry number ``attempt`` (1-based), capped."""

    if attempt < 1:
        raise ValueError("attempt is 1-based")
    return min(RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), RETRY_BACKOFF_CAP_SECONDS)


def total_retry_backoff(max_retries: int) -> float:
    """Total sleep introduced by ``max_retries`` retries."""

    if max_retries < 0:
        raise ValueError("max_retries must not be negative")
    return sum(backoff_seconds(attempt) for attempt in range(1, max_retries + 1))


def worst_case_provider_seconds(*, timeout: float, max_retries: int) -> float:
    """The longest a single provider call can take, including retries and their backoff.

    ``(max_retries + 1)`` attempts, because the first try is not a retry: with the shipped
    defaults that is ``18 × 2 + 0.25 = 36.25 s``, which fits inside the 45 s request budget —
    the point of the check in :mod:`app.config`. Before the Phase 1 review the validation
    only compared ``timeout < budget``, so a 30 s timeout with 2 retries needed up to 90 s
    and the provider timeout could never fire: every slow turn surfaced as a budget
    exhaustion instead, and ``LLM_MAX_RETRIES`` had no observable effect.
    """

    return timeout * (max_retries + 1) + total_retry_backoff(max_retries)


__all__ = [
    "RETRY_BACKOFF_BASE_SECONDS",
    "RETRY_BACKOFF_CAP_SECONDS",
    "BucketState",
    "backoff_seconds",
    "consume",
    "new_bucket",
    "refill",
    "refill_rate_for",
    "total_retry_backoff",
    "worst_case_provider_seconds",
]
