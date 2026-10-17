"""Conversation lifecycle: thread resolution, history retrieval, `/reset` (PLAN.md §2.1,
§10 Phase 1 deliverable 9 / NN1).

Layer 3. It imports the *protocols* (layer 0/2) and never `agent/` — the history provider is
injected, which is how the agent layer stays clear of storage and the service layer stays clear
of the graph.

What it packages, so the handler stays thin:

* resolve the chat's conversation and LangGraph thread (creating revision 1 on first contact);
* read the retained history **from the checkpointed thread state** — `messages` is the audit
  transcript and is never read as history (decision 3);
* shape the prompt input: history plus this turn's message. The graph receives it ready-made
  (§2.2's layering rule);
* record the turn to the transcript;
* `/reset`: rotate the thread id rather than deleting checkpoints, so a pending approval or a
  resumable run on the old thread survives (decision 3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.llm.protocols import ChatMessage
from app.storage.stores import ConversationStore

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PreparedTurn:
    """Everything the handler needs to run one turn."""

    thread_id: str
    prompt_input: list[ChatMessage]
    history_length: int


class ConversationService:
    """Turn preparation and context lifecycle."""

    def __init__(self, store: ConversationStore, *, max_messages: int) -> None:
        self._store = store
        self._max_messages = max_messages

    async def prepare(self, *, user_id: int, chat_id: int, text: str) -> PreparedTurn:
        """Resolve the thread, read the history, and shape the prompt input."""

        thread_id = await self._store.thread_id(user_id=user_id, chat_id=chat_id)
        history = await self._store.history(chat_id=chat_id)
        # The cap is applied here rather than by the store: both backends must behave the same,
        # and the checkpoint holds the whole thread by design.
        if len(history) > self._max_messages:
            history = history[-self._max_messages :]

        prompt_input = [*history, ChatMessage(role="user", content=text)]
        _logger.info(
            "turn prepared",
            extra={
                "history_messages": len(history),
                "thread_id": thread_id,
                "status": "prepared",
            },
        )
        return PreparedTurn(
            thread_id=thread_id, prompt_input=prompt_input, history_length=len(history)
        )

    async def record(
        self,
        *,
        chat_id: int,
        user_text: str,
        assistant_text: str,
        telegram_update_id: int | None = None,
    ) -> None:
        """Append the turn to the audit transcript."""

        await self._store.record_turn(
            chat_id=chat_id,
            user_text=user_text,
            assistant_text=assistant_text,
            telegram_update_id=telegram_update_id,
        )

    async def reset(self, *, chat_id: int) -> str:
        """`/reset`: rotate the context revision and return the new thread id."""

        new_thread = await self._store.reset(chat_id=chat_id)
        _logger.info("context reset", extra={"chat_id": chat_id, "status": "reset"})
        return new_thread


__all__ = ["ConversationService", "PreparedTurn"]
