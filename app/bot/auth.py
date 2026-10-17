"""Per-request authorisation (PLAN.md §7.2).

Thin by design: the decision logic and the allow-list parsing live in the pure layer 0 module
:mod:`app.policy.authz`; this module only *applies* them to an incoming update and raises the
typed error. Keeping the decision pure is what lets §9.1's authorisation tests run with no
credentials and no application wiring.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import Settings
from app.errors import UnauthorisedUser
from app.policy.authz import TelegramIdentity, extract_identity, is_authorized

_logger = logging.getLogger(__name__)


def authorize(update: dict[str, Any], settings: Settings) -> TelegramIdentity:
    """Return the caller's identity, or raise :class:`UnauthorisedUser`.

    Identity is ``effective_user.id`` (plus the chat id) and nothing else — never the visible
    name, the username or the message text (spec §6).
    """

    identity = extract_identity(update)
    if not is_authorized(
        identity,
        allowed_user_ids=settings.telegram_allowed_user_ids,
        app_env=settings.app_env,
        dev_allow_any_user=settings.dev_allow_any_user,
    ):
        raise UnauthorisedUser(
            "update from a user that is not authorised",
            # Only the numeric id is logged: it is the identifier the operator needs to
            # fix the allow-list, and it is not personal content.
            context={"telegram_user_id": None if identity is None else identity.user_id},
        )
    assert identity is not None  # is_authorized returns False for None
    return identity


__all__ = ["TelegramIdentity", "authorize"]
