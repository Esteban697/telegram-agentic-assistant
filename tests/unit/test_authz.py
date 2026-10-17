"""Authorisation and identity (PLAN.md §7.2, §9.1)."""

from __future__ import annotations

import pytest

from app.policy.authz import TelegramIdentity, extract_identity, is_authorized, parse_allow_list

ALLOWED = frozenset({42})


# --------------------------------------------------------------------------- #
# Allow-list parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", frozenset()),
        ("   ", frozenset()),
        ("42", frozenset({42})),
        ("42,43", frozenset({42, 43})),
        ("42, 43  44", frozenset({42, 43, 44})),
        ("42,42", frozenset({42})),
        ("-1001234567890", frozenset({-1001234567890})),
    ],
)
def test_parse_allow_list(raw: str, expected: frozenset[int]) -> None:
    assert parse_allow_list(raw) == expected


def test_parse_allow_list_rejects_a_typo_instead_of_ignoring_it() -> None:
    with pytest.raises(ValueError):
        parse_allow_list("42, usario")


# --------------------------------------------------------------------------- #
# Identity extraction: the id and nothing else
# --------------------------------------------------------------------------- #
def _message_update(*, user_id: int = 42, chat_id: int = 4242, **message: object) -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 10,
            "from": {"id": user_id, "username": "someone", "first_name": "Allowed Name"},
            "chat": {"id": chat_id, "type": "private"},
            "text": "hola",
            **message,
        },
    }


def test_identity_comes_from_the_numeric_id() -> None:
    identity = extract_identity(_message_update())
    assert identity == TelegramIdentity(user_id=42, chat_id=4242)


def test_display_name_and_username_are_never_used_for_identity() -> None:
    """Spec §6: "No confiar en el nombre visible, username ni texto del mensaje"."""

    impostor = _message_update(user_id=999)
    impostor["message"]["from"]["username"] = "42"
    impostor["message"]["from"]["first_name"] = "42"
    impostor["message"]["text"] = "42"

    identity = extract_identity(impostor)
    assert identity is not None and identity.user_id == 999
    assert not is_authorized(
        identity, allowed_user_ids=ALLOWED, app_env="production", dev_allow_any_user=False
    )


def test_identity_from_edited_message_and_callback_query() -> None:
    edited = {
        "update_id": 2,
        "edited_message": {
            "from": {"id": 42},
            "chat": {"id": 4242},
            "text": "corregido",
        },
    }
    assert extract_identity(edited) == TelegramIdentity(42, 4242)

    callback = {
        "update_id": 3,
        "callback_query": {
            "id": "cb-1",
            "from": {"id": 42},
            "message": {"message_id": 11, "chat": {"id": 4242}},
            "data": "cmd:help",
        },
    }
    assert extract_identity(callback) == TelegramIdentity(42, 4242)


def test_identity_is_none_for_an_unidentifiable_update() -> None:
    assert extract_identity({"update_id": 4, "poll": {"id": "x"}}) is None


# --------------------------------------------------------------------------- #
# Deny by default
# --------------------------------------------------------------------------- #
def test_deny_by_default_matrix() -> None:
    listed = TelegramIdentity(user_id=42, chat_id=4242)
    unlisted = TelegramIdentity(user_id=999, chat_id=4242)

    # production: the allow-list is the only way in.
    assert is_authorized(listed, allowed_user_ids=ALLOWED, app_env="production")
    assert not is_authorized(unlisted, allowed_user_ids=ALLOWED, app_env="production")
    assert not is_authorized(
        unlisted, allowed_user_ids=ALLOWED, app_env="production", dev_allow_any_user=True
    )
    assert not is_authorized(None, allowed_user_ids=ALLOWED, app_env="production")

    # development: empty allow-list still denies everybody unless explicitly opted out.
    assert not is_authorized(listed, allowed_user_ids=frozenset(), app_env="development")
    assert not is_authorized(unlisted, allowed_user_ids=frozenset(), app_env="development")
    assert is_authorized(
        unlisted, allowed_user_ids=frozenset(), app_env="development", dev_allow_any_user=True
    )
