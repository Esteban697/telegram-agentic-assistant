"""Execution limits: the token bucket and the one-notice-per-window rule (PLAN.md §7.6, A12)."""

from __future__ import annotations

import pytest

from app.api.telegram_webhook import RateLimiter
from app.policy.limits import BucketState, consume, new_bucket, refill, refill_rate_for

CAPACITY = 3
RATE = refill_rate_for(CAPACITY)  # 3 per minute = 0.05 tokens/second


def test_bucket_starts_full_and_allows_capacity_requests() -> None:
    state = new_bucket(CAPACITY, now=0.0)
    for _ in range(CAPACITY):
        allowed, state = consume(state, capacity=CAPACITY, refill_per_second=RATE, now=0.0)
        assert allowed


def test_bucket_denies_past_capacity() -> None:
    state = new_bucket(CAPACITY, now=0.0)
    for _ in range(CAPACITY):
        _, state = consume(state, capacity=CAPACITY, refill_per_second=RATE, now=0.0)
    allowed, state = consume(state, capacity=CAPACITY, refill_per_second=RATE, now=0.0)
    assert not allowed
    assert state.tokens == pytest.approx(0.0)


def test_bucket_refills_with_elapsed_time() -> None:
    state = BucketState(tokens=0.0, updated_at=0.0)
    # 20 seconds at 0.05 tokens/second = 1 token.
    state = refill(state, capacity=CAPACITY, refill_per_second=RATE, now=20.0)
    assert state.tokens == pytest.approx(1.0, abs=1e-9)
    allowed, _ = consume(state, capacity=CAPACITY, refill_per_second=RATE, now=20.0)
    assert allowed


def test_bucket_never_exceeds_capacity() -> None:
    state = BucketState(tokens=0.0, updated_at=0.0)
    state = refill(state, capacity=CAPACITY, refill_per_second=RATE, now=10_000.0)
    assert state.tokens == CAPACITY


def test_denied_request_does_not_reset_the_clock() -> None:
    """Hammering a depleted bucket must not push the refill horizon forward."""

    state = BucketState(tokens=0.0, updated_at=0.0)
    denied, state = consume(state, capacity=CAPACITY, refill_per_second=RATE, now=5.0)
    assert not denied
    assert state.updated_at == 5.0
    assert state.tokens == pytest.approx(5.0 * RATE)


def test_invalid_parameters_are_rejected() -> None:
    with pytest.raises(ValueError):
        new_bucket(0, now=0.0)
    with pytest.raises(ValueError):
        refill_rate_for(0)
    with pytest.raises(ValueError):
        consume(
            new_bucket(1, now=0.0), capacity=1, refill_per_second=1.0, now=0.0, cost=0
        )


# --------------------------------------------------------------------------- #
# The limiter as the webhook uses it
# --------------------------------------------------------------------------- #
def test_limiter_allows_then_denies_with_a_single_notice() -> None:
    limiter = RateLimiter(per_minute=2)

    assert limiter.check(1, now=0.0) == (True, False)
    assert limiter.check(1, now=0.0) == (True, False)
    assert limiter.check(1, now=0.0) == (False, True)  # first denial -> notify
    assert limiter.check(1, now=0.0) == (False, False)  # still denied, no second notice


def test_limiter_notice_recovers_after_the_window() -> None:
    limiter = RateLimiter(per_minute=60)  # 1 token per second

    for _ in range(60):
        limiter.check(1, now=0.0)
    assert limiter.check(1, now=0.0) == (False, True)
    assert limiter.check(1, now=1.1) == (True, False)
    assert limiter.check(1, now=1.1) == (False, True)  # notice re-armed after an allowed call


def test_limiter_is_per_user() -> None:
    limiter = RateLimiter(per_minute=1)
    assert limiter.check(1, now=0.0) == (True, False)
    assert limiter.check(2, now=0.0) == (True, False)
    assert limiter.check(1, now=0.0) == (False, True)
