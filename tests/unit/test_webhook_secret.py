"""Webhook secret validation (PLAN.md §7.1, N11)."""

from __future__ import annotations

import pytest

from app.api.telegram_webhook import _secret_matches

SECRET = "s3cret_token-ABC"


@pytest.mark.parametrize(
    ("provided", "expected"),
    [
        (SECRET, True),
        (SECRET + "x", False),
        (SECRET[:-1], False),
        ("", False),
        ("S3CRET_TOKEN-ABC", False),
    ],
)
def test_secret_comparison(provided: str, expected: bool) -> None:
    assert _secret_matches(provided, SECRET) is expected


def test_missing_header_is_denied_not_raised() -> None:
    assert _secret_matches(None, SECRET) is False


def test_empty_expected_secret_denies_everything() -> None:
    assert _secret_matches(SECRET, "") is False


def test_non_ascii_header_is_a_clean_denial() -> None:
    """N11: ``hmac.compare_digest`` raises ``TypeError`` on non-ASCII ``str``.

    A crafted header must produce the documented 403, never a 500 with a traceback, so the
    comparison works over encoded bytes and any failure is a denial.
    """

    assert _secret_matches("secreto-ñ", SECRET) is False
    assert _secret_matches("🔑", SECRET) is False
    assert _secret_matches("token with-separator", SECRET) is False


def test_surrogate_escaped_input_does_not_raise() -> None:
    assert _secret_matches("\udcff\udcfe", SECRET) is False
