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
from app.agent.prompts import DEFAULT_IMAGE_QUESTION, VISION_SYSTEM_PROMPT
from app.bot import messages
from app.bot.keyboards import (
    approval_keyboard,
    is_approval_callback,
    main_menu_keyboard,
    parse_callback_data,
)
from app.policy import approval as approval_machine
from app.policy.approval import ActionState
from app.bot.telegram_client import TelegramClient
from app.config import Settings
from app.errors import AssistantError
from app.llm.protocols import ChatMessage, ChatModel
from app.policy.authz import TelegramIdentity
from app.services.conversation import ConversationService
from app.storage.stores import (
    STATUS_FAILED,
    STATUS_IGNORED,
    STATUS_NOTICED,
    STATUS_PROCESSED,
    DedupeStore,
)
from app.services.approvals import ApprovalService
from app.services.work_queue import WorkQueue
from app.tools.memory import MemoryTools
from app.tools.registry import ToolRegistry
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
        approvals: ApprovalService | None = None,
        work_queue: WorkQueue | None = None,
        registry: ToolRegistry | None = None,
        ingestion: Any | None = None,
        document_tools: Any | None = None,
        vision_model: ChatModel | None = None,
    ) -> None:
        self._settings = settings
        self._telegram = telegram
        self._agent = agent
        self._conversations = conversations
        self._memory = memory_tools
        self._tasks = task_tools
        self._approvals = approvals
        self._queue = work_queue
        self._registry = registry
        self._ingestion = ingestion
        self._documents = document_tools
        self._vision = vision_model

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

        # Images are their own path: they are answered by the multimodal cloud model and never
        # enter the tool graph, so the image bytes cannot land in a checkpoint.
        photo = _largest_photo(message.get("photo"))
        if photo is not None:
            return await self._handle_image(
                identity,
                photo,
                caption=_text_of(message) or "",
                update_id=update.get("update_id"),
            )

        # **The document branch comes first.** `_text_of` returns `caption` when there is no
        # `text`, so a PDF sent with a caption ("aquí tienes el contrato") took the text branch
        # and the file was silently ignored — documented Telegram behaviour, not an edge case.
        document = message.get("document")
        if isinstance(document, dict):
            if str(document.get("mime_type") or "").startswith("image/"):
                return await self._handle_image(
                    identity,
                    document,
                    caption=_text_of(message) or "",
                    update_id=update.get("update_id"),
                )
            if self._ingestion is not None:
                return await self._ingest_document(identity, document)
            await self._reply(identity.chat_id, messages.render("documents.unavailable"))
            return HandlerOutcome(status=STATUS_IGNORED, replied=True, detail="documents_disabled")

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

    async def _ingest_document(
        self, identity: TelegramIdentity, document: dict[str, Any]
    ) -> HandlerOutcome:
        """Spec §9 stage 1: a document arrives by Telegram, and the pipeline indexes it."""

        from app.errors import CapabilityUnavailable

        assert self._ingestion is not None
        try:
            downloaded = await self._telegram.get_file(
                file_id=str(document.get("file_id", "")),
                max_bytes=self._settings.max_upload_mb * 1024 * 1024,
            )
            outcome = await self._ingestion.ingest(
                user_id=identity.user_id,
                filename=str(document.get("file_name") or "documento.pdf"),
                data=downloaded.content,
                declared_mime=str(document.get("mime_type") or "application/octet-stream"),
            )
        except CapabilityUnavailable as exc:
            await self._reply(identity.chat_id, messages.render(exc.message_key))
            return HandlerOutcome(status=STATUS_IGNORED, replied=True, detail=exc.code)
        except AssistantError as exc:
            _logger.warning("document ingest failed", extra=exc.as_log_fields())
            await self._reply(identity.chat_id, messages.render(exc.message_key))
            return HandlerOutcome(status=STATUS_FAILED, replied=True, detail=exc.code)

        if outcome.deduplicated:
            await self._reply(identity.chat_id, messages.render("documents.already_indexed"))
            return HandlerOutcome(status=STATUS_PROCESSED, replied=True, detail="deduplicated")

        if outcome.status != "ready":
            # The loser of a concurrent `ON CONFLICT` race on the same bytes comes back
            # `status="pending", chunk_count=0`, and the inline budget running out comes back
            # `pending` too. Falling through to "He indexado «x»: 0 fragmentos" told the user the
            # document was searchable when nothing had been written yet — and if the winner's
            # ingest then failed, that claim was never corrected.
            await self._reply(identity.chat_id, messages.render("documents.queued"))
            return HandlerOutcome(
                status=STATUS_PROCESSED, replied=True, detail=f"not_ready:{outcome.status}"
            )

        await self._reply(
            identity.chat_id,
            messages.render(
                "documents.indexed",
                name=str(document.get("file_name") or "documento.pdf"),
                chunks=outcome.chunk_count,
                pages=outcome.page_count or 0,
            ),
        )
        return HandlerOutcome(status=STATUS_PROCESSED, replied=True, detail="ingested")

    async def _handle_edited_message(
        self, update: dict[str, Any], identity: TelegramIdentity
    ) -> HandlerOutcome:
        """An edit to an already-processed message is noticed, never replayed (PLAN.md §7.7)."""

        await self._reply(identity.chat_id, messages.render("edited.notice"))
        return HandlerOutcome(status=STATUS_NOTICED, replied=True, detail="edited_message")

    async def _run_turn(
        self, text: str, identity: TelegramIdentity, *, update_id: int | None
    ) -> HandlerOutcome:
        prepared = None
        answered = False
        # Only the caller that actually opened the watchdog row may close it: closing the *other*
        # turn's row on the busy path would mark an in-flight turn as finished.
        owns_turn = False
        try:
            prepared = await self._conversations.prepare(
                user_id=identity.user_id, chat_id=identity.chat_id, text=text
            )

            # The watchdog goes in BEFORE the graph runs (RB1). A death anywhere inside the turn
            # therefore leaves a queued resume rather than a lost message — and a second message
            # arriving now finds the row `pending`, which is how (h) declines it.
            if self._queue is not None:
                claim = await self._queue.open_turn(
                    conversation_id=await self._conversations.conversation_id(
                        user_id=identity.user_id, chat_id=identity.chat_id
                    ),
                    thread_id=prepared.thread_id,
                    chat_id=identity.chat_id,
                )
                if claim.is_busy:
                    await self._reply(
                        identity.chat_id, messages.render("busy.previous_turn_in_progress")
                    )
                    return HandlerOutcome(
                        status=STATUS_IGNORED, replied=True, detail="busy_previous_turn"
                    )
                owns_turn = True

            result = await self._agent.run(
                prepared.prompt_input,
                thread_id=prepared.thread_id,
                user_id=identity.user_id,
                chat_id=identity.chat_id,
            )

            pending = await self._agent.pending_approval(prepared.thread_id)
            if pending is not None:
                # A level-2 call was prepared, not performed: the prompt is the reply.
                await self._send_approval_prompt(identity, pending)
                answered = True
                return HandlerOutcome(
                    status=STATUS_PROCESSED, replied=True, detail="approval_requested"
                )

            reply_text = result.content.strip() or messages.render("error.generic")
            await self._reply(identity.chat_id, reply_text)
            answered = True

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
        except AssistantError as exc:
            _logger.warning("conversation turn failed", extra=exc.as_log_fields())
            await self._reply(identity.chat_id, messages.render(exc.message_key))
            answered = True
            return HandlerOutcome(status=STATUS_FAILED, replied=True, detail=exc.code)
        finally:
            # Closed unconditionally (Phase 3 review): a path that skipped this left the row
            # `pending` for ever, and every later message in that chat was then declined as busy.
            # `answered` distinguishes a real reply from an escape.
            if owns_turn:
                await self._close_turn(prepared, failed=not answered)

    async def _handle_image(
        self,
        identity: TelegramIdentity,
        item: dict[str, Any],
        *,
        caption: str,
        update_id: int | None,
    ) -> HandlerOutcome:
        """Answer an image + caption with the multimodal cloud model.

        This path deliberately bypasses the tool graph: image turns only answer questions. The
        image bytes are never stored or logged; only the caption and the answer are recorded.
        """

        if self._vision is None:
            await self._reply(identity.chat_id, messages.render("images.unavailable"))
            return HandlerOutcome(status=STATUS_IGNORED, replied=True, detail="vision_disabled")

        file_id = str(item.get("file_id") or "")
        if not file_id:
            await self._reply(identity.chat_id, messages.render("images.unavailable"))
            return HandlerOutcome(status=STATUS_FAILED, replied=True, detail="image_no_file_id")

        max_bytes = self._settings.max_image_mb * 1024 * 1024
        try:
            downloaded = await self._telegram.get_file(file_id=file_id, max_bytes=max_bytes)
        except AssistantError as exc:
            _logger.warning("image download failed", extra=exc.as_log_fields())
            key = (
                "images.too_large"
                if "cap" in exc.detail or "above the" in exc.detail
                else "images.unavailable"
            )
            await self._reply(identity.chat_id, messages.render(key, max_mb=self._settings.max_image_mb))
            return HandlerOutcome(status=STATUS_FAILED, replied=True, detail=exc.code)

        mime = _sniff_image_mime(downloaded.content)
        if mime is None:
            await self._reply(identity.chat_id, messages.render("images.unsupported"))
            return HandlerOutcome(status=STATUS_PROCESSED, replied=True, detail="unsupported_image")

        prompt = caption.strip() or DEFAULT_IMAGE_QUESTION
        prepared = None
        owns_turn = False
        answered = False
        try:
            prepared = await self._conversations.prepare(
                user_id=identity.user_id,
                chat_id=identity.chat_id,
                text=f"[imagen] {prompt}",
            )
            if self._queue is not None:
                claim = await self._queue.open_turn(
                    conversation_id=await self._conversations.conversation_id(
                        user_id=identity.user_id, chat_id=identity.chat_id
                    ),
                    thread_id=prepared.thread_id,
                    chat_id=identity.chat_id,
                )
                if claim.is_busy:
                    await self._reply(
                        identity.chat_id, messages.render("busy.previous_turn_in_progress")
                    )
                    return HandlerOutcome(
                        status=STATUS_IGNORED, replied=True, detail="busy_previous_turn"
                    )
                owns_turn = True

            prompt_messages = [
                ChatMessage(role="system", content=VISION_SYSTEM_PROMPT),
                *prepared.prompt_input[:-1],
                ChatMessage(role="user", content=prompt, images=(downloaded.content,)),
            ]
            result = await self._vision.chat(
                prompt_messages,
                tools=None,
                timeout=self._settings.vision_timeout_seconds,
            )
            reply_text = result.content.strip() or messages.render("images.unreadable")
            await self._reply(identity.chat_id, reply_text)
            answered = True
            try:
                await self._conversations.record(
                    chat_id=identity.chat_id,
                    user_text=f"[imagen] {prompt}",
                    assistant_text=reply_text,
                    telegram_update_id=update_id,
                )
            except AssistantError as exc:  # pragma: no cover - reply already delivered
                _logger.warning("could not record the image turn", extra=exc.as_log_fields())
            _logger.info(
                "vision turn complete",
                extra={
                    "model": self._vision.model,
                    "mime": mime,
                    "image_bytes": len(downloaded.content),
                    "duration_ms": result.duration_ms,
                    "status": "ok",
                },
            )
            return HandlerOutcome(status=STATUS_PROCESSED, replied=True, detail="image_answered")
        except AssistantError as exc:
            _logger.warning("vision turn failed", extra=exc.as_log_fields())
            await self._reply(identity.chat_id, messages.render("images.unavailable"))
            answered = True
            return HandlerOutcome(status=STATUS_FAILED, replied=True, detail=exc.code)
        finally:
            if owns_turn:
                await self._close_turn(prepared, failed=not answered)

    async def _close_turn(self, prepared: Any, *, failed: bool = False) -> None:
        if self._queue is None or prepared is None:
            return
        try:
            await self._queue.close_turn(thread_id=prepared.thread_id, failed=failed)
        except AssistantError as exc:  # pragma: no cover - bookkeeping only
            _logger.warning("could not close the watchdog row", extra=exc.as_log_fields())

    async def _send_approval_prompt(
        self, identity: TelegramIdentity, pending: dict[str, Any]
    ) -> None:
        """Render the frozen preview and the two buttons. Nothing has run."""

        sent = await self._telegram.send_message(
            chat_id=identity.chat_id,
            text=messages.render(
                "approval.prompt",
                preview=pending.get("preview_text", ""),
                minutes=self._settings.approval_ttl_minutes,
            ),
            reply_markup=approval_keyboard(
                action_id=str(pending["action_id"]),
                approve=True,
                args_revision=int(pending["args_revision"]),
                args_hash=str(pending["args_hash"]),
            ),
        )
        if self._approvals is not None:
            await self._approvals.attach_message(
                action_id=str(pending["action_id"]), message_id=sent.message_id
            )

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
        data = str(callback.get("data", ""))

        # Approval buttons are handled before the command menu: they carry an action id, not a
        # command name, and the acknowledgement must fire first (N2).
        if is_approval_callback(data):
            if callback_id:
                await self._telegram.answer_callback_query(callback_query_id=callback_id)
            return await self._handle_approval(data, identity, callback)

        command = parse_callback_data(data)

        # The acknowledgement is answered first and neutrally (PLAN.md §4.2, N2): an answer sent
        # before the action cannot report the action's result, so the outcome is its own message.
        if callback_id:
            await self._telegram.answer_callback_query(callback_query_id=callback_id)

        if command is None:
            await self._reply(identity.chat_id, messages.render("callback.not_available"))
            return HandlerOutcome(status=STATUS_IGNORED, replied=True, detail="unknown_callback")

        return await self._run_command(command, "", identity)

    # ------------------------------------------------------------------ #
    # Approvals
    # ------------------------------------------------------------------ #
    async def _handle_approval(
        self, data: str, identity: TelegramIdentity, callback: dict[str, Any]
    ) -> HandlerOutcome:
        """Handle an Aprobar/Rechazar press.

        The acknowledgement has already been sent by the caller, so a slow claim cannot make
        Telegram retry; the outcome is communicated by editing the decision message.
        """

        parsed = approval_machine.parse_callback_data(data)
        if parsed is None or self._approvals is None:
            await self._reply(identity.chat_id, messages.render("approval.unknown_action"))
            return HandlerOutcome(status=STATUS_IGNORED, replied=True, detail="bad_approval_data")

        if not parsed.approve:
            decision = await self._approvals.decide(
                callback=parsed, user_id=identity.user_id, chat_id=identity.chat_id
            )
            key = "approval.rejected" if decision.applied else decision.message_key
            await self._reply(identity.chat_id, messages.render(key))
            return HandlerOutcome(
                status=STATUS_PROCESSED, replied=True, detail=f"rejected:{decision.applied}"
            )

        decision = await self._approvals.decide(
            callback=parsed, user_id=identity.user_id, chat_id=identity.chat_id
        )
        if not decision.applied:
            await self._reply(identity.chat_id, messages.render(decision.message_key))
            return HandlerOutcome(
                status=STATUS_PROCESSED, replied=True, detail=f"not_decided:{decision.message_key}"
            )

        # The decision landed; now try to consume it. Two repeated callbacks race here and
        # exactly one wins (the CAS), which is what the spec's headline sentence is about.
        try:
            execution = await self._approvals.claim_and_execute(
                callback=parsed,
                user_id=identity.user_id,
                chat_id=identity.chat_id,
                executor=f"handler:{identity.user_id}",
            )
        except AssistantError as exc:
            _logger.error("approved arguments refused", extra=exc.as_log_fields())
            await self._reply(identity.chat_id, messages.render(exc.message_key))
            return HandlerOutcome(status=STATUS_FAILED, replied=True, detail=exc.code)

        if not execution.claimed:
            await self._reply(identity.chat_id, messages.render(execution.message_key))
            return HandlerOutcome(
                status=STATUS_PROCESSED, replied=True, detail=f"not_claimed:{execution.cause}"
            )

        await self._reply(identity.chat_id, messages.render("approval.executing"))
        return await self._execute_approved(identity, execution)

    async def _execute_approved(self, identity: TelegramIdentity, execution: Any) -> HandlerOutcome:
        assert self._approvals is not None
        action_id = str(execution.action_id)
        if self._registry is None:  # pragma: no cover - always wired
            await self._approvals.finish(
                action_id=action_id, state=ActionState.FAILED, error="no registry"
            )
            await self._reply(identity.chat_id, messages.render("error.generic"))
            return HandlerOutcome(status=STATUS_FAILED, replied=True, detail="no_registry")

        try:
            result = await self._registry.execute_approved(
                execution=execution, user_id=identity.user_id
            )
        except AssistantError as exc:
            # §4.2 step 3: an ambiguous outcome is `unknown`, never `failed`. A provider timeout
            # does not tell us whether the far side completed the operation, and the plan forbids
            # assuming it did not ("No asumir que un timeout significa que la acción falló").
            # Inverted default (Phase 3 review, §7): a code we do not recognise is treated as
            # **ambiguous**, because the plan forbids reporting an unknown outcome as a failure.
            unambiguous = {
                "capability_unavailable",
                "invalid_arguments",
                "tool_not_allowed",
                "unsupported_document",
            }
            ambiguous = exc.code not in unambiguous
            state = ActionState.UNKNOWN if ambiguous else ActionState.FAILED
            await self._approvals.finish(
                action_id=action_id,
                state=state,
                result={"reason": "timeout"} if ambiguous else {"error_code": exc.code},
                error=exc.code,
            )
            _logger.warning(
                "approved action outcome is ambiguous" if ambiguous else "approved action failed",
                extra=exc.as_log_fields(),
            )
            await self._reply(
                identity.chat_id,
                messages.render("approval.unknown" if ambiguous else exc.message_key),
            )
            return HandlerOutcome(
                status=STATUS_FAILED,
                replied=True,
                detail=f"{'unknown' if ambiguous else 'failed'}:{exc.code}",
            )

        if result.ok:
            await self._approvals.finish(
                action_id=action_id, state=ActionState.SUCCEEDED, result={"data": result.data}
            )
            await self._reply(
                identity.chat_id,
                messages.render("approval.succeeded", detail=_short(result.data)),
            )
            return HandlerOutcome(status=STATUS_PROCESSED, replied=True, detail="executed")

        # A capability gap is a definite failure, not an ambiguity: nothing left this system.
        await self._approvals.finish(
            action_id=action_id,
            state=ActionState.FAILED,
            result={"error_code": result.error_code},
            error=result.error_code,
        )
        if result.error_code == "CapabilityUnavailable":
            # A capability gap is a definite failure, not an ambiguity: nothing left this system.
            await self._reply(identity.chat_id, messages.render("approval.capability_unavailable"))
        else:
            await self._reply(
                identity.chat_id,
                messages.render("approval.failed", detail=result.detail or "error"),
            )
        return HandlerOutcome(
            status=STATUS_PROCESSED, replied=True, detail=f"executed_failed:{result.error_code}"
        )

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


def _largest_photo(value: Any) -> dict[str, Any] | None:
    """Telegram sends several sizes; the largest is the useful one."""

    if not isinstance(value, list):
        return None
    photos = [item for item in value if isinstance(item, dict) and item.get("file_id")]
    if not photos:
        return None

    def score(item: dict[str, Any]) -> tuple[int, int]:
        size = item.get("file_size")
        width = item.get("width")
        height = item.get("height")
        return (
            int(size) if isinstance(size, int) else 0,
            int(width) * int(height)
            if isinstance(width, int) and isinstance(height, int)
            else 0,
        )

    return max(photos, key=score)


def _sniff_image_mime(data: bytes) -> str | None:
    """Only accept the formats the cloud vision model can read."""

    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


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


def _short(data: Any, limit: int = 200) -> str:
    import json

    if data is None:
        return ""
    rendered = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)
    return rendered if len(rendered) <= limit else rendered[:limit] + "…"


__all__ = ["HandlerOutcome", "TelegramHandlers"]
