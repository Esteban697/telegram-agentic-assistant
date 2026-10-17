"""Update handling: commands, conversation turns, callbacks (PLAN.md §10 Phase 1 deliverable 7,
Phase 2: `/tasks`, `/memory` and `/forget` become real).

This is where the plan's prompt-assembly rule lives (§2.2, §10 Phase 1 deliverable 9): **the
handler reads the conversation service and passes a shaped message list to the agent**, which
is why ``agent/`` never imports ``app.storage``.

Phase 2 changes:

* history comes from the checkpointed thread state, so the handler no longer appends to a list —
  it calls :meth:`ConversationService.prepare` and :meth:`ConversationService.record`;
* `/reset` rotates the thread id instead of clearing a list (decision 3);
* `/tasks`, `/memory` and `/forget` operate on real storage. When no database is configured
  (`STORE_BACKEND=memory`, development), they answer with the explicit
  ``error.storage_unavailable`` message rather than pretending to have stored something.

Everything the user reads is rendered from :mod:`app.bot.messages`.
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
from app.services.conversation import ConversationService
from app.storage.stores import (
    STATUS_FAILED,
    STATUS_IGNORED,
    STATUS_NOTICED,
    STATUS_PROCESSED,
    DedupeStore,
)
from app.tools.memory import MemoryTools
from app.tools.tasks import ListTasksArgs, TaskTools

_logger = logging.getLogger(__name__)

_KNOWN_COMMANDS = {"start", "help", "reset", "tasks", "memory", "forget"}

# Mirrors the `memories.key` CHECK constraint (1..128); the handler checks it so the user gets
# a catalogue message instead of a validation error.
MAX_MEMORY_KEY_CHARS = 128


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
        conversations: ConversationService,
        memory_tools: MemoryTools | None = None,
        task_tools: TaskTools | None = None,
    ) -> None:
        self._settings = settings
        self._telegram = telegram
        self._agent = agent
        self._conversations = conversations
        self._memory = memory_tools
        self._tasks = task_tools

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

        command, argument = _parse_command(text)
        if command is not None:
            return await self._run_command(
                command, argument, identity, update_id=update.get("update_id")
            )

        return await self._run_turn(text, identity, update_id=update.get("update_id"))

    async def _handle_edited_message(
        self, update: dict[str, Any], identity: TelegramIdentity
    ) -> HandlerOutcome:
        """An edit to an already-processed message is noticed, never replayed (PLAN.md §7.7)."""

        await self._reply(identity.chat_id, messages.render("edited.notice"))
        return HandlerOutcome(status=STATUS_NOTICED, replied=True, detail="edited_message")

    async def _run_turn(
        self, text: str, identity: TelegramIdentity, *, update_id: int | None
    ) -> HandlerOutcome:
        try:
            prepared = await self._conversations.prepare(
                user_id=identity.user_id, chat_id=identity.chat_id, text=text
            )
            result = await self._agent.run(prepared.prompt_input, thread_id=prepared.thread_id)
        except AssistantError as exc:
            _logger.warning("conversation turn failed", extra=exc.as_log_fields())
            await self._reply(identity.chat_id, messages.render(exc.message_key))
            return HandlerOutcome(status=STATUS_FAILED, replied=True, detail=exc.code)

        reply_text = result.content.strip() or messages.render("error.generic")
        await self._reply(identity.chat_id, reply_text)

        try:
            # The transcript is an audit log, never read back as history (decision 3).
            await self._conversations.record(
                chat_id=identity.chat_id,
                user_text=text,
                assistant_text=reply_text,
                telegram_update_id=update_id,
            )
        except AssistantError as exc:  # pragma: no cover - the reply is already delivered
            _logger.warning("could not record the turn", extra=exc.as_log_fields())

        return HandlerOutcome(status=STATUS_PROCESSED, replied=True, detail="answered")

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    async def _run_command(
        self,
        command: str,
        argument: str,
        identity: TelegramIdentity,
        *,
        update_id: int | None = None,
    ) -> HandlerOutcome:
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
            try:
                await self._conversations.reset(chat_id=identity.chat_id)
            except AssistantError as exc:
                _logger.warning("reset failed", extra=exc.as_log_fields())
                await self._reply(identity.chat_id, messages.render(exc.message_key))
                return HandlerOutcome(status=STATUS_FAILED, replied=True, detail=exc.code)
            await self._reply(identity.chat_id, messages.render("reset.done"))
            return HandlerOutcome(status=STATUS_PROCESSED, replied=True, detail="reset")

        if command == "tasks":
            return await self._show_tasks(identity)

        if command == "memory":
            return await self._show_memories(identity)

        if command == "forget":
            return await self._forget(argument, identity)

        await self._reply(identity.chat_id, messages.render("help"))
        return HandlerOutcome(status=STATUS_PROCESSED, replied=True, detail="unknown_command")

    async def _show_tasks(self, identity: TelegramIdentity) -> HandlerOutcome:
        if self._tasks is None:
            await self._reply(identity.chat_id, messages.render("error.storage_unavailable"))
            return HandlerOutcome(status=STATUS_IGNORED, replied=True, detail="no_storage")

        result = await self._tasks.list_tasks(user_id=identity.user_id, args=ListTasksArgs())
        items = result.data or []
        if not items:
            await self._reply(identity.chat_id, messages.render("tasks.empty"))
        else:
            lines = [messages.render("tasks.header")]
            lines.extend(
                messages.render(
                    "tasks.line",
                    title=item["title"],
                    local=item["local"],
                    timezone=item["timezone"],
                    state=messages.state_label(str(item["state"])),
                )
                for item in items
            )
            await self._reply(identity.chat_id, "\n".join(lines))
        return HandlerOutcome(status=STATUS_PROCESSED, replied=True, detail="tasks_listed")

    async def _show_memories(self, identity: TelegramIdentity) -> HandlerOutcome:
        if self._memory is None:
            await self._reply(identity.chat_id, messages.render("error.storage_unavailable"))
            return HandlerOutcome(status=STATUS_IGNORED, replied=True, detail="no_storage")

        result = await self._memory.list_memories(user_id=identity.user_id)
        items = result.data or []
        if not items:
            await self._reply(identity.chat_id, messages.render("memory.empty"))
        else:
            lines = [messages.render("memory.header")]
            lines.extend(
                messages.render("memory.line", key=item["key"], value=item["value"])
                for item in items
            )
            await self._reply(identity.chat_id, "\n".join(lines))
        return HandlerOutcome(status=STATUS_PROCESSED, replied=True, detail="memory_listed")

    async def _forget(self, argument: str, identity: TelegramIdentity) -> HandlerOutcome:
        """`/forget <clave>` deletes one memory; `/forget all [confirm]` needs confirmation.

        Spec §6: "/forget debe permitir eliminar una memoria explícita ... con confirmación
        cuando el alcance sea amplio". A two-step command is used rather than a callback, so
        the confirmation does not depend on the approval machinery that arrives in Phase 3.
        """

        if self._memory is None:
            await self._reply(identity.chat_id, messages.render("error.storage_unavailable"))
            return HandlerOutcome(status=STATUS_IGNORED, replied=True, detail="no_storage")

        target = argument.strip()
        if not target:
            await self._reply(identity.chat_id, messages.render("forget.needs_key"))
            return HandlerOutcome(status=STATUS_PROCESSED, replied=True, detail="forget_needs_key")

        if target.split()[0].lower() == "all":
            return await self._forget_all(target, identity)

        # A key longer than the column allows cannot exist, so answer instead of letting a
        # pydantic ValidationError escape to the webhook's catch-all (which turned it into
        # "Ha ocurrido un error inesperado").
        if len(target) > MAX_MEMORY_KEY_CHARS:
            await self._reply(identity.chat_id, messages.render("forget.not_found", key=target[:64]))
            return HandlerOutcome(status=STATUS_PROCESSED, replied=True, detail="forget_key_too_long")

        result = await self._memory.delete_memory_by_key(user_id=identity.user_id, key=target)
        if (result.data or {}).get("deleted"):
            await self._reply(identity.chat_id, messages.render("forget.done", key=target))
            return HandlerOutcome(status=STATUS_PROCESSED, replied=True, detail="memory_deleted")

        await self._reply(identity.chat_id, messages.render("forget.not_found", key=target))
        return HandlerOutcome(status=STATUS_PROCESSED, replied=True, detail="forget_not_found")

    async def _forget_all(self, target: str, identity: TelegramIdentity) -> HandlerOutcome:
        parts = target.split()

        if len(parts) > 1 and parts[1].lower() == "confirm":
            result = await self._memory.delete_all_memories(user_id=identity.user_id)
            deleted = int((result.data or {}).get("deleted", 0))
            await self._reply(identity.chat_id, messages.render("forget.all_done", count=deleted))
            return HandlerOutcome(
                status=STATUS_PROCESSED, replied=True, detail="memory_deleted_all"
            )

        counted = await self._memory.count_memories(user_id=identity.user_id)
        count = int((counted.data or {}).get("count", 0))
        await self._reply(identity.chat_id, messages.render("forget.confirm_all", count=count))
        return HandlerOutcome(status=STATUS_PROCESSED, replied=True, detail="forget_needs_confirm")

    # ------------------------------------------------------------------ #
    # Callbacks
    # ------------------------------------------------------------------ #
    async def _handle_callback(
        self, update: dict[str, Any], identity: TelegramIdentity
    ) -> HandlerOutcome:
        callback = update.get("callback_query") or {}
        callback_id = str(callback.get("id", ""))
        command = parse_callback_data(str(callback.get("data", "")))

        # The acknowledgement is answered first and neutrally (PLAN.md §4.2, N2): an answer sent
        # before the action cannot report the action's result, so the outcome is its own message.
        if callback_id:
            await self._telegram.answer_callback_query(callback_query_id=callback_id)

        if command is None:
            await self._reply(identity.chat_id, messages.render("callback.not_available"))
            return HandlerOutcome(status=STATUS_IGNORED, replied=True, detail="unknown_callback")

        return await self._run_command(command, "", identity)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    async def notify(self, chat_id: int, text: str) -> None:
        """Send a catalogue-rendered notice outside the command/turn flow."""

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


def _parse_command(text: str) -> tuple[str | None, str]:
    """Return ``(command, argument)`` for ``/name`` or ``/name@bot arg``."""

    stripped = text.strip()
    if not stripped.startswith("/"):
        return None, ""
    head, _, rest = stripped.partition(" ")
    name = head[1:].split("@", 1)[0].lower()
    return (name or None), rest.strip()


__all__ = ["HandlerOutcome", "TelegramHandlers"]
