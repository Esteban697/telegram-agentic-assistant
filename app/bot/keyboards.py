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


__all__ = ["CALLBACK_PREFIX", "main_menu_keyboard", "parse_callback_data"]
