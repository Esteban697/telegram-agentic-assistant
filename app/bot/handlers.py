"""Update handling: commands, conversation turns, callbacks (PLAN.md §10 Phase 1 deliverable 7).

This is where the plan's prompt-assembly rule lives (§2.2, §10 Phase 1 deliverable 9): **the
handler reads the conversation store and passes a shaped message list to the agent**, which
is why ``agent/`` never imports ``app.storage.stores``. Phase 2 swaps the store for an adapter
over ``aget_state`` and this module does not change.

Everything the user reads is rendered from :mod:`app.bot.messages`; this module contains no
user-facing string literals.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.agent.graph import ChatAgent
from app.bot import messages
from app.bot.keyboards import main_menu_keyboard, parse_callback_data
from app.bot.telegram_client import TelegramClient
from app.config import Settings
from app.errors import AssistantError
from app.llm.protocols import ChatMessage
from app.policy.authz import TelegramIdentity
from app.storage.stores import (
    STATUS_FAILED,
    STATUS_IGNORED,
    STATUS_NOTICED,
    STATUS_PROCESSED,
    ConversationStore,
)

_logger = logging.getLogger(__name__)

# Commands whose implementation belongs to a later phase. They answer on the Phase 1 command
# surface with the "not available yet" message so that the surface does not change later
# (PLAN.md §10 Phase 1 deliverable 7).
_PENDING_COMMANDS = {"tasks", "memory", "forget"}
_KNOWN_COMMANDS = {"start", "help", "reset"} | _PENDING_COMMANDS


@dataclass(frozen=True, slots=True)
class HandlerOutcome:
    """What happened to an update, so the caller can record it in the ledger."""

    status: str
    replied: bool
    detail: str = ""


class TelegramHandlers:
    """Dispatch normalised updates to commands, the agent, or an explicit refusal."""

    def __init__(
        self,
        *,
        settings: Settings,
        telegram: TelegramClient,
        agent: ChatAgent,
        conversations: ConversationStore,
    ) -> None:
        self._settings = settings
        self._telegram = telegram
        self._agent = agent
        self._conversations = conversations

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #
    async def handle(self, update: dict[str, Any], identity: TelegramIdentity) -> HandlerOutcome:
        if "callback_query" in update:
            return await self._handle_callback(update, identity)
        if "edited_message" in update:
            return await self._handle_edited_message(update, identity)
        if "message" in update:
            return await self._handle_message(update, identity)
        _logger.info(
            "unsupported update type",
            extra={"status": STATUS_IGNORED, "update_keys": sorted(update.keys())},
        )
        return HandlerOutcome(status=STATUS_IGNORED, replied=False, detail="unsupported_type")

    # ------------------------------------------------------------------ #
    # Messages
    # ------------------------------------------------------------------ #
    async def _handle_message(
        self, update: dict[str, Any], identity: TelegramIdentity
    ) -> HandlerOutcome:
        message = update.get("message") or {}
        text = _text_of(message)

        if text is None:
            await self._reply(identity.chat_id, messages.render("unsupported.content"))
            return HandlerOutcome(status=STATUS_PROCESSED, replied=True, detail="unsupported_content")

        if len(text) > self._settings.max_message_chars:
            await self._reply(identity.chat_id, messages.render("error.message_too_long"))
            return HandlerOutcome(status=STATUS_IGNORED, replied=True, detail="message_too_long")

        command = _parse_command(text)
        if command is not None:
            return await self._run_command(command, identity)

        return await self._run_turn(text, identity)

    async def _handle_edited_message(
        self, update: dict[str, Any], identity: TelegramIdentity
    ) -> HandlerOutcome:
        """An edit to an already-processed message is noticed, never replayed (PLAN.md §7.7).

        Re-running the turn would risk a second level-1 side effect and would make the
        transcript disagree with the graph's history; the user is told to resend instead.
        """

        await self._reply(identity.chat_id, messages.render("edited.notice"))
        return HandlerOutcome(status=STATUS_NOTICED, replied=True, detail="edited_message")

    async def _run_turn(self, text: str, identity: TelegramIdentity) -> HandlerOutcome:
        history = await self._conversations.get(identity.chat_id)
        user_message = ChatMessage(role="user", content=text)
        # The shaped prompt input: history (already retained) plus this turn's message.
        prompt_input = [*history, user_message]

        _logger.info(
            "conversation turn start",
            extra={"history_messages": len(history), "model": self._agent.model_name},
        )

        try:
            result = await self._agent.run(prompt_input)
        except AssistantError as exc:
            _logger.warning("conversation turn failed", extra=exc.as_log_fields())
            await self._reply(identity.chat_id, messages.render(exc.message_key))
            return HandlerOutcome(status=STATUS_FAILED, replied=True, detail=exc.code)

        reply_text = result.content.strip() or messages.render("error.generic")
        await self._reply(identity.chat_id, reply_text)
        await self._conversations.append(
            identity.chat_id,
            [user_message, ChatMessage(role="assistant", content=reply_text)],
        )
        return HandlerOutcome(status=STATUS_PROCESSED, replied=True, detail="answered")

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    async def _run_command(self, command: str, identity: TelegramIdentity) -> HandlerOutcome:
        if command in _PENDING_COMMANDS:
            await self._reply(
                identity.chat_id,
                messages.render("error.not_available", command=f"/{command}"),
            )
            return HandlerOutcome(status=STATUS_PROCESSED, replied=True, detail=f"pending:{command}")

        if command == "start":
            await self._reply(
                identity.chat_id, messages.render("start"), keyboard=main_menu_keyboard()
            )
            return HandlerOutcome(status=STATUS_PROCESSED, replied=True, detail="start")

        if command == "help":
            await self._reply(
                identity.chat_id, messages.render("help"), keyboard=main_menu_keyboard()
            )
            return HandlerOutcome(status=STATUS_PROCESSED, replied=True, detail="help")

        if command == "reset":
            # Phase 1: drop this chat's retained context. Nothing else exists to protect yet
            # (no documents, no memories), and Phase 2 replaces the body with thread rotation
            # behind the same handler and the same reply key (PLAN.md §10 Phase 1 deliverable 7).
            await self._conversations.reset(identity.chat_id)
            await self._reply(identity.chat_id, messages.render("reset.done"))
            return HandlerOutcome(status=STATUS_PROCESSED, replied=True, detail="reset")

        # Unknown command: answer on the command surface rather than staying silent.
        await self._reply(identity.chat_id, messages.render("help"))
        return HandlerOutcome(status=STATUS_PROCESSED, replied=True, detail="unknown_command")

    # ------------------------------------------------------------------ #
    # Callbacks
    # ------------------------------------------------------------------ #
    async def _handle_callback(
        self, update: dict[str, Any], identity: TelegramIdentity
    ) -> HandlerOutcome:
        callback = update.get("callback_query") or {}
        callback_id = str(callback.get("id", ""))
        command = parse_callback_data(str(callback.get("data", "")))

        # The acknowledgement is answered first and neutrally, because Telegram expects a
        # prompt answer and an answer sent before the action cannot report the action's result
        # (PLAN.md §4.2, N2). The outcome is then sent as its own message.
        if callback_id:
            await self._telegram.answer_callback_query(callback_query_id=callback_id)

        if command is None:
            await self._reply(identity.chat_id, messages.render("callback.not_available"))
            return HandlerOutcome(status=STATUS_IGNORED, replied=True, detail="unknown_callback")

        return await self._run_command(command, identity)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    async def notify(self, chat_id: int, text: str) -> None:
        """Send a catalogue-rendered notice outside the command/turn flow.

        Used by the webhook for its rate-limit and error notices, so that no other module
        needs to reach into this object's private helpers.
        """

        await self._telegram.send_message(chat_id=chat_id, text=text)

    async def _reply(
        self, chat_id: int, text: str, *, keyboard: dict[str, Any] | None = None
    ) -> None:
        await self._telegram.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)


def _text_of(message: dict[str, Any]) -> str | None:
    for key in ("text", "caption"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _parse_command(text: str) -> str | None:
    """Return the bare command name for ``/name`` or ``/name@bot``, else None."""

    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    head = stripped.split()[0][1:]
    head = head.split("@", 1)[0].lower()
    return head if head else None


__all__ = ["HandlerOutcome", "TelegramHandlers"]
