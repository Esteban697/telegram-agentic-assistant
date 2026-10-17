"""Authorisation: allow-list parsing, identity extraction, deny-by-default (PLAN.md §7.2).

Layer 0 and **pure**: it parses and decides, it does not log or send anything.

The identity rule is the load-bearing one: identity is ``effective_user.id`` only. The
visible name, the username and the message text are never used for authorisation (spec §6:
"No confiar en el nombre visible, username ni texto del mensaje como identidad").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def parse_allow_list(raw: str) -> frozenset[int]:
    """Parse ``"1, 2  3"`` into ``{1, 2, 3}``.

    Raises :class:`ValueError` on anything non-numeric so a typo cannot silently become an
    empty allow-list (which would lock everybody out, or — if emptiness were ignored —
    admit everybody).
    """

    if raw.strip() == "":
        return frozenset()

    parts = [part for part in raw.replace(",", " ").split() if part]
    parsed: set[int] = set()
    for part in parts:
        try:
            parsed.add(int(part))
        except ValueError as exc:
            raise ValueError(f"TELEGRAM_ALLOWED_USER_IDS contains a non-numeric entry: {part!r}") from exc
    return frozenset(parsed)


@dataclass(frozen=True, slots=True)
class TelegramIdentity:
    """The two ids that identify the caller. Nothing else is trusted."""

    user_id: int
    chat_id: int


def extract_identity(update: dict[str, Any]) -> TelegramIdentity | None:
    """Extract the caller's identity from a Telegram update.

    Handles ``message``, ``edited_message`` and ``callback_query``; returns ``None`` for an
    update that carries no identifiable sender, which the caller treats as "handle
    deliberately and record", never as "allow".
    """

    for key in ("message", "edited_message", "channel_post"):
        payload = update.get(key)
        if isinstance(payload, dict):
            user_id = _int_or_none(payload.get("from", {}).get("id") if isinstance(payload.get("from"), dict) else None)
            chat_id = _int_or_none(payload.get("chat", {}).get("id") if isinstance(payload.get("chat"), dict) else None)
            if user_id is not None and chat_id is not None:
                return TelegramIdentity(user_id=user_id, chat_id=chat_id)

    callback = update.get("callback_query")
    if isinstance(callback, dict):
        user_id = _int_or_none(callback.get("from", {}).get("id") if isinstance(callback.get("from"), dict) else None)
        message = callback.get("message")
        chat_id = None
        if isinstance(message, dict) and isinstance(message.get("chat"), dict):
            chat_id = _int_or_none(message["chat"].get("id"))
        if user_id is not None and chat_id is not None:
            return TelegramIdentity(user_id=user_id, chat_id=chat_id)

    return None


def is_authorized(
    identity: TelegramIdentity | None,
    *,
    allowed_user_ids: frozenset[int],
    app_env: str,
    dev_allow_any_user: bool = False,
) -> bool:
    """Deny by default.

    ``dev_allow_any_user`` is a local-testing affordance only; :class:`~app.config.Settings`
    refuses to start with it enabled in production, so this function does not have to repeat
    that check — but it also refuses to honour the flag itself when ``app_env`` is
    ``production``, so a caller that bypasses the settings layer still cannot widen access.
    """

    if identity is None:
        return False
    if app_env == "production":
        return identity.user_id in allowed_user_ids
    if identity.user_id in allowed_user_ids:
        return True
    return bool(dev_allow_any_user)


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


__all__ = ["TelegramIdentity", "extract_identity", "is_authorized", "parse_allow_list"]
