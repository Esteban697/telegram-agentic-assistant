"""Store protocols and their Phase 1 in-memory implementations (PLAN.md §2.1 item 8, §10
Phase 1 deliverable 10; module path and layer fixed in round 1, N14).

**Layer 2.** Imports only layer 0 and 1, so it may use ``app.llm.protocols`` for the message
type and must never import ``services``, ``tools``, ``agent``, ``bot``, ``api`` or ``jobs``.

Two things this module is deliberately **not**:

* It is not durable and not multi-instance safe. ``STORE_BACKEND=memory`` is a production
  startup error (PLAN.md §7.2, B6), so this cannot be deployed by accident; the Phase 2
  PostgreSQL implementations arrive behind these same protocols.
* It is not a timer. Nothing here schedules work: TTLs are evaluated on access, and the
  production reminder mechanism is a database scan driven by Cloud Scheduler (spec §11).

The in-memory implementations live on after Phase 1 as test doubles.
"""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable, Sequence
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.errors import AssistantError
from app.llm.protocols import ChatMessage
from app.storage import repositories

# Statuses for the inbound ledger. These mirror the Phase 2 ``inbox_updates.status`` values
# (received / processed / ignored / failed / noticed) so the in-memory store is a faithful
# stand-in and the tests written against it keep their meaning when the table arrives.
STATUS_RECEIVED = "received"
STATUS_PROCESSED = "processed"
STATUS_IGNORED = "ignored"
STATUS_FAILED = "failed"
STATUS_NOTICED = "noticed"


@runtime_checkable
class DedupeStore(Protocol):
    """The inbound update ledger (the in-memory stand-in for ``inbox_updates``).

    ``claim`` is the idempotency mechanism: it must return ``True`` exactly once per
    ``update_id``, so a Telegram retry or a double delivery is processed a single time
    (PLAN.md §5.1).
    """

    async def claim(self, update_id: int, *, kind: str) -> bool:
        """Return True if this update was newly claimed, False if it is a duplicate."""
        ...

    async def mark(self, update_id: int, *, status: str, error_code: str | None = None) -> None:
        """Record the outcome for an already-claimed update."""
        ...

    async def status_of(self, update_id: int) -> str | None:
        """Return the recorded status, or None if the update was never seen."""
        ...


@runtime_checkable
class HistoryProvider(Protocol):
    """Supplies conversational history for a thread.

    Deliberately structural: `ChatAgent` satisfies it, and because the *storage* layer declares
    the protocol rather than importing the agent, ``agent/`` still never imports
    ``app.storage`` (PLAN.md §2.2, §10 Phase 1 deliverable 9).
    """

    async def history(self, thread_id: str) -> list[ChatMessage]:
        """Return the retained history for a thread, oldest first."""
        ...


@runtime_checkable
class ConversationRegistry(Protocol):
    """Maps a Telegram chat to its current conversation and LangGraph thread."""

    async def resolve(self, *, user_id: int, chat_id: int) -> str:
        """Return the active thread id, creating it if needed."""
        ...

    async def rotate(self, *, chat_id: int) -> str:
        """Start a new context revision and return the new thread id."""
        ...


@runtime_checkable
class ConversationStore(Protocol):
    """Conversational context for prompt assembly.

    Read and written by the **handler**, never by ``agent/``. Phase 2 (NN1): the history comes
    from the checkpointed thread state, and the audit rows are written but never read back —
    decision 3 keeps one source of truth.
    """

    async def history(self, *, chat_id: int) -> list[ChatMessage]:
        """Return the retained history for a chat, oldest first."""
        ...

    async def thread_id(self, *, user_id: int, chat_id: int) -> str:
        """Return the LangGraph thread id for this chat."""
        ...

    async def reset(self, *, chat_id: int) -> str:
        """Rotate the context revision; returns the new thread id."""
        ...

    async def conversation_id(self, *, user_id: int, chat_id: int) -> Any:
        """Resolve (creating if needed) and return the conversation's id.

        `GraphConversationStore` is the only implementation that can answer this, and it does so
        through the registry it already holds.
        """

        resolver = getattr(self._registry, "conversation_id", None)
        if resolver is None:
            return None
        return await resolver(user_id=user_id, chat_id=chat_id)

    async def record_turn(
        self,
        *,
        chat_id: int,
        user_text: str,
        assistant_text: str,
        telegram_update_id: int | None = None,
    ) -> None:
        """Append the turn to the audit transcript (never read back as history)."""
        ...


class InMemoryDedupeStore:
    """Bounded, TTL'd set of seen update ids.

    Eviction is oldest-first at ``max_entries``; entries older than ``ttl_seconds`` are
    treated as unseen, which mirrors a ledger whose retention window has passed.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int,
        max_entries: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        # update_id -> (stamp, kind, status, error_code)
        self._entries: OrderedDict[int, tuple[float, str, str, str | None]] = OrderedDict()

    async def claim(self, update_id: int, *, kind: str) -> bool:
        now = self._clock()
        self._evict(now)
        if update_id in self._entries:
            return False
        self._entries[update_id] = (now, kind, STATUS_RECEIVED, None)
        self._entries.move_to_end(update_id)
        if len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
        return True

    async def mark(self, update_id: int, *, status: str, error_code: str | None = None) -> None:
        now = self._clock()
        existing = self._entries.get(update_id)
        kind = existing[1] if existing else "unknown"
        self._entries[update_id] = (now, kind, status, error_code)

    async def status_of(self, update_id: int) -> str | None:
        self._evict(self._clock())
        entry = self._entries.get(update_id)
        return None if entry is None else entry[2]

    async def kind_of(self, update_id: int) -> str | None:
        """Return the recorded ``kind`` for an update (diagnostics and tests)."""

        self._evict(self._clock())
        entry = self._entries.get(update_id)
        return None if entry is None else entry[1]

    def __len__(self) -> int:
        self._evict(self._clock())
        return len(self._entries)

    def _evict(self, now: float) -> None:
        if self._ttl <= 0:
            self._entries.clear()
            return
        while self._entries:
            oldest_id, (stamp, _kind, _status, _error) = next(iter(self._entries.items()))
            if now - stamp < self._ttl:
                break
            self._entries.pop(oldest_id, None)


class InMemoryConversationStore:
    """Per-chat message list with a hard cap and a TTL.

    **After Phase 2 this is a test double, not the wiring.** The handler resolves history
    through :class:`GraphConversationStore`, which reads the checkpointed thread state; the
    in-memory list survives because the unit tests for the protocol need a trivial
    implementation, and because `STORE_BACKEND=memory` development uses an in-memory
    *checkpointer* rather than this list.
    """

    def __init__(
        self,
        *,
        max_messages: int,
        ttl_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_messages = max_messages
        self._ttl = ttl_seconds
        self._clock = clock
        self._chats: dict[int, tuple[float, deque[ChatMessage]]] = {}

    async def get(self, chat_id: int) -> list[ChatMessage]:
        entry = self._live_entry(chat_id)
        return list(entry[1]) if entry else []

    async def append(self, chat_id: int, messages: Sequence[ChatMessage]) -> None:
        now = self._clock()
        entry = self._chats.get(chat_id)
        if entry is None or self._expired(entry[0], now):
            history: deque[ChatMessage] = deque(maxlen=self._max_messages)
        else:
            history = entry[1]
        history.extend(messages)
        self._chats[chat_id] = (now, history)

    async def reset(self, chat_id: int) -> None:
        self._chats.pop(chat_id, None)

    def _live_entry(self, chat_id: int) -> tuple[float, deque[ChatMessage]] | None:
        entry = self._chats.get(chat_id)
        if entry is None:
            return None
        if self._expired(entry[0], self._clock()):
            self._chats.pop(chat_id, None)
            return None
        return entry

    def _expired(self, stamp: float, now: float) -> bool:
        return self._ttl > 0 and (now - stamp) >= self._ttl


# --------------------------------------------------------------------------- #
# Phase 2: the registry, and the store that reads history from the checkpointer
# --------------------------------------------------------------------------- #
class InMemoryConversationRegistry:
    """Chat -> thread mapping held in a dict (development without a database)."""

    def __init__(self) -> None:
        self._threads: dict[int, str] = {}

    async def resolve(self, *, user_id: int, chat_id: int) -> str:
        thread_id = self._threads.get(chat_id)
        if thread_id is None:
            thread_id = f"local-{uuid.uuid4()}"
            self._threads[chat_id] = thread_id
        return thread_id

    async def rotate(self, *, chat_id: int) -> str:
        thread_id = f"local-{uuid.uuid4()}"
        self._threads[chat_id] = thread_id
        return thread_id


class SqlConversationRegistry:
    """Chat -> thread mapping in PostgreSQL, with `/reset` as a context-revision rotation."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def resolve(self, *, user_id: int, chat_id: int) -> str:
        async with self._sessions() as session, session.begin():
            conversation = await repositories.get_or_create_active_conversation(
                session, telegram_user_id=user_id, telegram_chat_id=chat_id
            )
            await repositories.touch_conversation(session, conversation_id=conversation.id)
            return conversation.thread_id

    async def conversation_id(self, *, user_id: int, chat_id: int) -> Any:
        async with self._sessions() as session, session.begin():
            conversation = await repositories.get_or_create_active_conversation(
                session, telegram_user_id=user_id, telegram_chat_id=chat_id
            )
            return conversation.id

    async def rotate(self, *, chat_id: int) -> str:
        async with self._sessions() as session, session.begin():
            conversation = await repositories.get_active_conversation(
                session, telegram_chat_id=chat_id
            )
            if conversation is None:
                # Nothing to rotate: the next resolve() will create revision 1.
                return ""
            fresh = await repositories.rotate_conversation_context(
                session, conversation=conversation
            )
            return fresh.thread_id


class GraphConversationStore:
    """The Phase 2 `ConversationStore`: history from the checkpoint, transcript for the audit.

    This is the "thin adapter over `aget_state`" of NN1. `messages` is written but never read
    as history, so the graph's state and the transcript cannot diverge in a way that affects
    behaviour.
    """

    def __init__(
        self,
        registry: ConversationRegistry,
        history_provider: HistoryProvider,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._registry = registry
        self._history = history_provider
        self._sessions = session_factory
        # The thread id is remembered per chat, so `history()` never has to resolve one itself.
        # It used to call the registry with `user_id=0`, which would have created the chat's
        # conversation row attributed to Telegram user 0 if `history()` ran first (item 12).
        self._threads: dict[int, str] = {}

    async def thread_id(self, *, user_id: int, chat_id: int) -> str:
        thread_id = await self._registry.resolve(user_id=user_id, chat_id=chat_id)
        self._threads[chat_id] = thread_id
        return thread_id

    async def history(self, *, chat_id: int) -> list[ChatMessage]:
        thread_id = self._threads.get(chat_id)
        if thread_id is None:
            # Impossible rather than unlikely: resolving a thread here would need a user id,
            # and guessing one writes a row with the wrong owner.
            raise AssistantError(
                "history() called before thread_id() for this chat",
                context={"chat_id": chat_id},
            )
        return await self._history.history(thread_id)

    async def reset(self, *, chat_id: int) -> str:
        thread_id = await self._registry.rotate(chat_id=chat_id)
        self._threads[chat_id] = thread_id
        return thread_id

    async def conversation_id(self, *, user_id: int, chat_id: int) -> Any:
        """The conversation row's id, for rows that reference it (the watchdog)."""

        resolver = getattr(self._registry, "conversation_id", None)
        if resolver is None:
            return None
        return await resolver(user_id=user_id, chat_id=chat_id)

    async def record_turn(
        self,
        *,
        chat_id: int,
        user_text: str,
        assistant_text: str,
        telegram_update_id: int | None = None,
    ) -> None:
        if self._sessions is None:
            return
        async with self._sessions() as session, session.begin():
            conversation = await repositories.get_active_conversation(
                session, telegram_chat_id=chat_id
            )
            if conversation is None:  # pragma: no cover - resolve() runs first
                return
            await repositories.append_message(
                session,
                conversation_id=conversation.id,
                role="user",
                content=user_text,
                telegram_update_id=telegram_update_id,
            )
            await repositories.append_message(
                session,
                conversation_id=conversation.id,
                role="assistant",
                content=assistant_text,
            )


class PostgresDedupeStore:
    """The durable inbound ledger. The primary key is the idempotency mechanism (§5.1)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def claim(self, update_id: int, *, kind: str) -> bool:
        async with self._sessions() as session, session.begin():
            return await repositories.claim_inbox_update(session, update_id=update_id, kind=kind)

    async def mark(self, update_id: int, *, status: str, error_code: str | None = None) -> None:
        async with self._sessions() as session, session.begin():
            await repositories.mark_inbox_update(
                session, update_id=update_id, status=status, error_code=error_code
            )

    async def status_of(self, update_id: int) -> str | None:
        async with self._sessions() as session:
            row = await repositories.get_inbox_update(session, update_id=update_id)
            return None if row is None else row.status

    async def kind_of(self, update_id: int) -> str | None:
        async with self._sessions() as session:
            row = await repositories.get_inbox_update(session, update_id=update_id)
            return None if row is None else row.kind


def build_stores(
    *,
    store_backend: str,
    app_env: str,
    update_dedupe_ttl_seconds: int,
    update_dedupe_max_entries: int,
    context_max_messages: int,
    context_ttl_seconds: int,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    history_provider: HistoryProvider | None = None,
) -> tuple[DedupeStore, ConversationStore]:
    """Choose the store implementations from configuration.

    The production refusal is asserted here as well as in configuration validation, so a
    caller that constructs stores directly cannot bypass the fail-fast rule (PLAN.md §7.2,
    B6, A13).

    Phase 2: ``postgres`` is the durable path. It needs a session factory and a history
    provider (the compiled graph), because conversational history comes from the checkpointed
    thread state rather than from a table.
    """

    if history_provider is None:
        raise ValueError(
            "a history provider is required: conversational history is read from the "
            "checkpointed thread state, not from a table (NN1)"
        )

    if store_backend == "memory":
        if app_env == "production":
            raise ValueError(
                "STORE_BACKEND=memory is not allowed in production (PLAN.md §7.2); "
                "the in-memory stores lose update-id idempotency across instances"
            )
        # Both backends expose the same ConversationStore semantics; only the *saver* and the
        # registry differ. That is what lets the same integration suite run against either.
        return (
            InMemoryDedupeStore(
                ttl_seconds=update_dedupe_ttl_seconds,
                max_entries=update_dedupe_max_entries,
            ),
            GraphConversationStore(
                InMemoryConversationRegistry(), history_provider, session_factory=None
            ),
        )

    if store_backend == "postgres":
        if session_factory is None:
            raise ValueError("STORE_BACKEND=postgres needs a session factory")
        return (
            PostgresDedupeStore(session_factory),
            GraphConversationStore(
                SqlConversationRegistry(session_factory),
                history_provider,
                session_factory=session_factory,
            ),
        )

    raise ValueError(f"unknown STORE_BACKEND: {store_backend!r}")


__all__ = [
    "STATUS_FAILED",
    "STATUS_IGNORED",
    "STATUS_NOTICED",
    "STATUS_PROCESSED",
    "STATUS_RECEIVED",
    "ConversationRegistry",
    "ConversationStore",
    "DedupeStore",
    "GraphConversationStore",
    "HistoryProvider",
    "InMemoryConversationRegistry",
    "InMemoryConversationStore",
    "InMemoryDedupeStore",
    "PostgresDedupeStore",
    "SqlConversationRegistry",
    "build_stores",
]

