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
from collections import OrderedDict, deque
from collections.abc import Callable, Sequence
from typing import Protocol, runtime_checkable

from app.llm.protocols import ChatMessage

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
class ConversationStore(Protocol):
    """Conversational context for prompt assembly.

    Read and written by the **handler**, never by ``agent/`` (PLAN.md §2.2, §10 Phase 1
    deliverable 9): the handler assembles the shaped prompt input and passes it in.
    """

    async def get(self, chat_id: int) -> list[ChatMessage]:
        """Return the retained history for a chat, oldest first."""
        ...

    async def append(self, chat_id: int, messages: Sequence[ChatMessage]) -> None:
        """Append turns, trimming to the configured cap."""
        ...

    async def reset(self, chat_id: int) -> None:
        """Drop the retained history for a chat (Phase 1 ``/reset``)."""
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
    """Per-chat message list with a hard cap and a TTL."""

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


def build_stores(
    *,
    store_backend: str,
    app_env: str,
    update_dedupe_ttl_seconds: int,
    update_dedupe_max_entries: int,
    context_max_messages: int,
    context_ttl_seconds: int,
) -> tuple[DedupeStore, ConversationStore]:
    """Choose the store implementations from configuration.

    The production refusal is asserted here as well as in configuration validation, so a
    caller that constructs stores directly cannot bypass the fail-fast rule (PLAN.md §7.2,
    B6, A13).
    """

    if store_backend == "memory":
        if app_env == "production":
            raise ValueError(
                "STORE_BACKEND=memory is not allowed in production (PLAN.md §7.2); "
                "the in-memory stores lose update-id idempotency across instances"
            )
        return (
            InMemoryDedupeStore(
                ttl_seconds=update_dedupe_ttl_seconds,
                max_entries=update_dedupe_max_entries,
            ),
            InMemoryConversationStore(
                max_messages=context_max_messages,
                ttl_seconds=context_ttl_seconds,
            ),
        )

    raise NotImplementedError(
        f"STORE_BACKEND={store_backend!r} arrives in Phase 2 (PostgreSQL, PLAN.md §10)"
    )


__all__ = [
    "STATUS_FAILED",
    "STATUS_IGNORED",
    "STATUS_NOTICED",
    "STATUS_PROCESSED",
    "STATUS_RECEIVED",
    "ConversationStore",
    "DedupeStore",
    "InMemoryConversationStore",
    "InMemoryDedupeStore",
    "build_stores",
]
