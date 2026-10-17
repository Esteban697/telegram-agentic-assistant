"""Store protocols and the in-memory implementations (PLAN.md §10 Phase 1 deliverable 10)."""

from __future__ import annotations

import pytest

from app.llm.protocols import ChatMessage
from app.storage.stores import (
    GraphConversationStore,
    STATUS_IGNORED,
    STATUS_NOTICED,
    STATUS_PROCESSED,
    InMemoryConversationStore,
    InMemoryDedupeStore,
    build_stores,
)


class FakeClock:
    """Injectable time, so TTL behaviour is deterministic and nothing sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --------------------------------------------------------------------------- #
# Dedupe / inbound ledger
# --------------------------------------------------------------------------- #
async def test_claim_is_true_exactly_once_per_update_id() -> None:
    store = InMemoryDedupeStore(ttl_seconds=60, max_entries=10)
    assert await store.claim(1, kind="message") is True
    assert await store.claim(1, kind="message") is False
    assert await store.claim(2, kind="message") is True


async def test_status_round_trip() -> None:
    store = InMemoryDedupeStore(ttl_seconds=60, max_entries=10)
    await store.claim(7, kind="message")
    assert await store.status_of(7) == "received"
    await store.mark(7, status=STATUS_IGNORED, error_code="unauthorised_user")
    assert await store.status_of(7) == STATUS_IGNORED
    assert await store.kind_of(7) == "message"


async def test_mark_records_an_update_that_was_never_claimed() -> None:
    """The ledger keeps an update that failed before it could be claimed."""

    store = InMemoryDedupeStore(ttl_seconds=60, max_entries=10)
    await store.mark(9, status=STATUS_NOTICED)
    assert await store.status_of(9) == STATUS_NOTICED


async def test_entries_expire_after_the_ttl() -> None:
    clock = FakeClock()
    store = InMemoryDedupeStore(ttl_seconds=60, max_entries=10, clock=clock)

    await store.claim(1, kind="message")
    clock.advance(59)
    assert await store.claim(1, kind="message") is False
    clock.advance(2)
    assert await store.claim(1, kind="message") is True


async def test_entries_are_evicted_oldest_first_at_the_cap() -> None:
    store = InMemoryDedupeStore(ttl_seconds=3600, max_entries=3)
    for update_id in (1, 2, 3):
        await store.claim(update_id, kind="message")
    await store.claim(4, kind="message")

    assert len(store) == 3
    assert await store.status_of(1) is None
    assert await store.status_of(4) == "received"


async def test_an_unknown_update_has_no_status() -> None:
    store = InMemoryDedupeStore(ttl_seconds=60, max_entries=10)
    assert await store.status_of(123) is None
    assert await store.kind_of(123) is None


# --------------------------------------------------------------------------- #
# Conversation context
# --------------------------------------------------------------------------- #
async def test_conversation_round_trip() -> None:
    store = InMemoryConversationStore(max_messages=10, ttl_seconds=60)
    await store.append(1, [ChatMessage(role="user", content="hola")])
    await store.append(1, [ChatMessage(role="assistant", content="buenas")])

    history = await store.get(1)
    assert [message.content for message in history] == ["hola", "buenas"]
    assert history[0].role == "user"


async def test_history_is_trimmed_to_the_configured_cap() -> None:
    store = InMemoryConversationStore(max_messages=3, ttl_seconds=60)
    for index in range(6):
        await store.append(1, [ChatMessage(role="user", content=f"m{index}")])

    history = await store.get(1)
    assert [message.content for message in history] == ["m3", "m4", "m5"]
    assert len(history) == 3


async def test_reset_drops_only_that_chat() -> None:
    store = InMemoryConversationStore(max_messages=10, ttl_seconds=60)
    await store.append(1, [ChatMessage(role="user", content="uno")])
    await store.append(2, [ChatMessage(role="user", content="dos")])

    await store.reset(1)
    assert await store.get(1) == []
    assert [message.content for message in await store.get(2)] == ["dos"]


async def test_context_expires_after_the_ttl() -> None:
    clock = FakeClock()
    store = InMemoryConversationStore(max_messages=10, ttl_seconds=60, clock=clock)
    await store.append(1, [ChatMessage(role="user", content="hola")])

    clock.advance(61)
    assert await store.get(1) == []


# --------------------------------------------------------------------------- #
# Selection and the production refusal (B6, A13)
# --------------------------------------------------------------------------- #
class _FakeHistory:
    async def history(self, thread_id: str) -> list[ChatMessage]:
        return []


def test_build_stores_returns_the_in_memory_implementations_outside_production() -> None:
    dedupe, conversations = build_stores(
        store_backend="memory",
        app_env="development",
        update_dedupe_ttl_seconds=60,
        update_dedupe_max_entries=10,
        context_max_messages=5,
        context_ttl_seconds=60,
        history_provider=_FakeHistory(),
    )
    assert isinstance(dedupe, InMemoryDedupeStore)
    # Phase 2: both backends expose the same ConversationStore semantics, because history
    # comes from the checkpointer; only the saver and the registry differ.
    assert isinstance(conversations, GraphConversationStore)


def test_build_stores_refuses_memory_in_production() -> None:
    with pytest.raises(ValueError, match="not allowed in production"):
        build_stores(
            store_backend="memory",
            app_env="production",
            update_dedupe_ttl_seconds=60,
            update_dedupe_max_entries=10,
            context_max_messages=5,
            context_ttl_seconds=60,
            history_provider=_FakeHistory(),
        )


def test_postgres_backend_requires_a_session_factory() -> None:
    """Phase 2 implements it; a missing dependency must fail loudly, not silently."""

    with pytest.raises(ValueError, match="session factory"):
        build_stores(
            store_backend="postgres",
            app_env="development",
            update_dedupe_ttl_seconds=60,
            update_dedupe_max_entries=10,
            context_max_messages=5,
            context_ttl_seconds=60,
            history_provider=_FakeHistory(),
        )


async def test_stores_satisfy_the_protocols() -> None:
    from app.storage.stores import ConversationStore, DedupeStore

    dedupe, conversations = build_stores(
        store_backend="memory",
        app_env="test",
        update_dedupe_ttl_seconds=60,
        update_dedupe_max_entries=10,
        context_max_messages=5,
        context_ttl_seconds=60,
        history_provider=_FakeHistory(),
    )
    assert isinstance(dedupe, DedupeStore)
    assert isinstance(conversations, ConversationStore)
    await dedupe.mark(1, status=STATUS_PROCESSED)
    assert await dedupe.status_of(1) == STATUS_PROCESSED


def test_a_history_provider_is_required() -> None:
    """History comes from the checkpointed thread state, never from a table (NN1)."""

    with pytest.raises(ValueError, match="history provider"):
        build_stores(
            store_backend="memory",
            app_env="development",
            update_dedupe_ttl_seconds=60,
            update_dedupe_max_entries=10,
            context_max_messages=5,
            context_ttl_seconds=60,
        )


async def test_the_memory_conversation_store_is_a_test_double_for_the_old_protocol() -> None:
    """Kept deliberately: the unit tests need a trivial get/append/reset implementation."""

    store = InMemoryConversationStore(max_messages=2, ttl_seconds=60)
    await store.append(1, [ChatMessage(role="user", content="a")])
    assert len(await store.get(1)) == 1
    await store.reset(1)
    assert await store.get(1) == []
