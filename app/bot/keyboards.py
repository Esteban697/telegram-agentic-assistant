"""Inline keyboards (PLAN.md §2.1, §10 Phase 1 deliverable 7).

Phase 1 ships the main menu, which is what gives ``answerCallbackQuery`` something real to
answer; Phase 3 adds the approval buttons, whose ``callback_data`` follows the
``ap:<action_id>:<a|r>:<args_revision>:<args_hash[:16]>`` format fixed in §4.1.

Callback data is capped by Telegram at 64 bytes, so Phase 1's ``cmd:<name>`` form is kept
short deliberately.
"""

from __future__ import annotations

from typing import Any

CALLBACK_PREFIX = "cmd:"
APPROVAL_PREFIX = "ap"

# The commands the menu exposes. `/tasks`, `/memory` and `/forget` work from Phase 2 but
# stay off the menu: the menu is for the two stateless commands, while the other three are
# useful mainly with an argument (`/forget <clave>`), which a button cannot supply.
_MENU: tuple[tuple[str, str], ...] = (
    ("/help", "help"),
    ("/reset", "reset"),
)


def main_menu_keyboard() -> dict[str, Any]:
    """Return a Telegram ``reply_markup`` with one row per menu entry."""

    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": f"{CALLBACK_PREFIX}{name}"}]
            for label, name in _MENU
        ]
    }


def approval_keyboard(
    *, action_id: str, approve: bool, args_revision: int, args_hash: str
) -> dict[str, Any]:
    """The two buttons of an approval prompt.

    `callback_data` is `ap:<action_id>:<a|r>:<args_revision>:<args_hash[:16]>` — 48 bytes, inside
    Telegram's 64-byte cap (PLAN.md §4.1). The full 32-byte hash does not fit, which is why the
    claim compares the row's *prefix*.
    """

    from app.policy.approval import build_callback_data

    return {
        "inline_keyboard": [
            [
                {
                    "text": "Aprobar",
                    "callback_data": build_callback_data(
                        action_id=action_id,
                        approve=True,
                        args_revision=args_revision,
                        args_hash=args_hash,
                    ),
                },
                {
                    "text": "Rechazar",
                    "callback_data": build_callback_data(
                        action_id=action_id,
                        approve=False,
                        args_revision=args_revision,
                        args_hash=args_hash,
                    ),
                },
            ]
        ]
    }


def is_approval_callback(data: str) -> bool:
    return data.startswith(f"{APPROVAL_PREFIX}:")


def parse_callback_data(data: str) -> str | None:
    """Return the command name carried by a menu callback, or None if it is not one.

    Only the two opaque values Phase 1 needs are trusted from a callback; the user, the chat
    and the command itself are re-derived server-side.
    """

    if not data.startswith(CALLBACK_PREFIX):
        return None
    name = data[len(CALLBACK_PREFIX) :].strip()
    if not name or len(data) > 64:
        return None
    known = {entry[1] for entry in _MENU}
    return name if name in known else None


__all__ = [
    "APPROVAL_PREFIX",
    "CALLBACK_PREFIX",
    "approval_keyboard",
    "is_approval_callback",
    "main_menu_keyboard",
    "parse_callback_data",
]
