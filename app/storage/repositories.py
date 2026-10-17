"""All SQL, in one place (PLAN.md §2.1, §10 Phase 2).

Layer 2. Imports layer 0-1 and the store protocols from the same package.

**The signatures here are frozen by Phase 2**: Phase 3 and Phase 4 build on them. Where a
Phase 2 caller needs a narrower shape, the narrow shape is added as a new function rather than
by editing these.

Four things in this module are load-bearing and were heavily reviewed:

* ``claim_due_tasks`` carries ``attempts < max_attempts`` **in SQL** (B1) — the cap is not
  advisory.
* ``search_chunks`` is the **single chokepoint** allowed to issue a vector ``ORDER BY``, with
  the owner predicate and the document-version predicate inside the SQL literal (§6.5, RB3).
* ``reconcile_stale_dispatches`` moves stale ``claimed`` rows to ``unknown`` and never
  re-sends (B2).
* Everything is ``READ COMMITTED`` (§4.6): each mutating statement re-checks its own predicate
  against the latest committed row, which is what makes the guarded ``UPDATE`` the mutual
  exclusion.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.policy.approval import RowView, canonical_args
from app.storage.models import (
    Conversation,
    Document,
    DocumentChunk,
    EmbeddingModel,
    InboxUpdate,
    Memory,
    Message,
    ReminderDispatch,
    Task,
)

_logger = logging.getLogger(__name__)

# Statuses written into the ledger (mirroring the CHECK constraint).
INBOX_RECEIVED = "received"
INBOX_PROCESSED = "processed"
INBOX_IGNORED = "ignored"
INBOX_FAILED = "failed"
INBOX_NOTICED = "noticed"

# The states a claim may take a task from.
TASK_PENDING = "pending"
TASK_COMPLETED = "completed"
TASK_CANCELLED = "cancelled"
TASK_FAILED = "failed"

DISPATCH_CLAIMED = "claimed"
DISPATCH_SENT = "sent"
DISPATCH_FAILED = "failed"
DISPATCH_UNKNOWN = "unknown"


# --------------------------------------------------------------------------- #
# Conversations
# --------------------------------------------------------------------------- #
def build_thread_id(conversation_id: uuid.UUID, revision: int) -> str:
    """The LangGraph thread key: one thread per (conversation, context revision)."""

    return f"{conversation_id}:{revision}"


async def get_active_conversation(
    session: AsyncSession, *, telegram_chat_id: int
) -> Conversation | None:
    result = await session.execute(
        sa.select(Conversation).where(
            Conversation.telegram_chat_id == telegram_chat_id,
            Conversation.status == "active",
        )
    )
    return result.scalar_one_or_none()


async def get_or_create_active_conversation(
    session: AsyncSession, *, telegram_user_id: int, telegram_chat_id: int
) -> Conversation:
    """Return the active conversation, creating it if this chat has none.

    The partial unique index on ``(telegram_chat_id) WHERE status='active'`` is what stops two
    concurrent first messages from creating two conversations: the loser of the race raises a
    unique violation, which is retried as a read.
    """

    existing = await get_active_conversation(session, telegram_chat_id=telegram_chat_id)
    if existing is not None:
        return existing

    # The id is generated client-side so no row is ever written with a placeholder thread id
    # and updated afterwards: `thread_id` is itself unique, so a placeholder would make two
    # concurrent inserts contend on that constraint as well.
    conversation_id = uuid.uuid4()
    conversation = Conversation(
        id=conversation_id,
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        thread_id=build_thread_id(conversation_id, 1),
        context_revision=1,
    )
    try:
        # A SAVEPOINT, not `session.rollback()`. Rolling back here deactivates the *caller's*
        # transaction, and the enclosing `session.begin()` then raises
        # `InvalidRequestError: Can't operate on closed transaction inside context manager` —
        # so the loser of the race got an unhandled error instead of the winner's row.
        async with session.begin_nested():
            session.add(conversation)
            await session.flush()
    except sa.exc.IntegrityError:
        # Lost the race. A unique violation blocks until the other transaction commits, so the
        # winner's row is visible to this read (READ COMMITTED gives each statement a fresh
        # snapshot), which is exactly what the docstring promises.
        again = await get_active_conversation(session, telegram_chat_id=telegram_chat_id)
        if again is None:  # pragma: no cover - the constraint exists, so this cannot happen
            raise
        return again
    return conversation


async def rotate_conversation_context(
    session: AsyncSession, *, conversation: Conversation
) -> Conversation:
    """`/reset`: mark the current context as reset and open a new revision.

    Rotating the thread id rather than deleting checkpoints is decision 3: a pending approval
    or a resumable run belongs to the old thread and must survive the reset.
    """

    conversation.status = "reset"
    await session.flush()

    revision = conversation.context_revision + 1
    fresh_id = uuid.uuid4()
    fresh = Conversation(
        id=fresh_id,
        telegram_user_id=conversation.telegram_user_id,
        telegram_chat_id=conversation.telegram_chat_id,
        thread_id=build_thread_id(fresh_id, revision),
        context_revision=revision,
    )
    try:
        async with session.begin_nested():
            session.add(fresh)
            await session.flush()
    except sa.exc.IntegrityError:
        # Two concurrent /reset calls: the loser adopts the winner's new revision rather than
        # failing. Same savepoint reasoning as above.
        winner = await get_active_conversation(
            session, telegram_chat_id=conversation.telegram_chat_id
        )
        if winner is None:  # pragma: no cover - the constraint exists
            raise
        return winner
    return fresh


async def touch_conversation(session: AsyncSession, *, conversation_id: uuid.UUID) -> None:
    await session.execute(
        sa.update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(last_activity_at=sa.func.now())
    )


# --------------------------------------------------------------------------- #
# Messages (audit only — never read back as history, decision 3)
# --------------------------------------------------------------------------- #
async def append_message(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    role: str,
    content: str,
    telegram_update_id: int | None = None,
    meta: dict[str, Any] | None = None,
) -> Message:
    message = Message(
        conversation_id=conversation_id,
        role=role,
        content=content,
        telegram_update_id=telegram_update_id,
        meta=meta or {},
    )
    session.add(message)
    await session.flush()
    return message


async def list_messages(
    session: AsyncSession, *, conversation_id: uuid.UUID, limit: int = 50
) -> list[Message]:
    result = await session.execute(
        sa.select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit)
    )
    return list(result.scalars())


# --------------------------------------------------------------------------- #
# Inbound ledger
# --------------------------------------------------------------------------- #
async def claim_inbox_update(session: AsyncSession, *, update_id: int, kind: str) -> bool:
    """Return True if this update was newly claimed.

    ``ON CONFLICT DO NOTHING`` on the primary key is the whole inbound idempotency mechanism
    (PLAN.md §5.1): a Telegram retry inserts nothing and returns no row.
    """

    statement = (
        pg_insert(InboxUpdate)
        .values(update_id=update_id, kind=kind, status=INBOX_RECEIVED)
        .on_conflict_do_nothing(index_elements=[InboxUpdate.update_id])
        .returning(InboxUpdate.update_id)
    )
    claimed = (await session.execute(statement)).scalar_one_or_none()
    return claimed is not None


async def mark_inbox_update(
    session: AsyncSession,
    *,
    update_id: int,
    status: str,
    conversation_id: uuid.UUID | None = None,
    error_code: str | None = None,
) -> None:
    statement = (
        pg_insert(InboxUpdate)
        .values(
            update_id=update_id,
            kind="unsupported",
            status=status,
            conversation_id=conversation_id,
            error_code=error_code,
        )
        .on_conflict_do_update(
            index_elements=[InboxUpdate.update_id],
            set_={
                "status": status,
                "conversation_id": sa.func.coalesce(
                    sa.text("excluded.conversation_id"), InboxUpdate.conversation_id
                ),
                "error_code": error_code,
            },
        )
    )
    await session.execute(statement)


async def get_inbox_update(session: AsyncSession, *, update_id: int) -> InboxUpdate | None:
    return await session.get(InboxUpdate, update_id)


# --------------------------------------------------------------------------- #
# Memories
# --------------------------------------------------------------------------- #
async def upsert_memory(
    session: AsyncSession,
    *,
    user_id: int,
    key: str,
    value: str,
    memory_type: str = "preference",
    source: str = "explicit_user",
    confidence: float = 1.0,
) -> Memory:
    """Save or replace a memory. The partial unique index is the conflict target."""

    statement = (
        pg_insert(Memory)
        .values(
            user_id=user_id,
            key=key,
            value=value,
            memory_type=memory_type,
            source=source,
            confidence=confidence,
            is_explicit=True,
        )
        .on_conflict_do_update(
            index_elements=[Memory.user_id, Memory.key],
            index_where=sa.text("deleted_at IS NULL"),
            set_={"value": value, "memory_type": memory_type, "updated_at": sa.func.now()},
        )
        .returning(Memory)
    )
    return (await session.execute(statement)).scalar_one()


def _escape_like(value: str) -> str:
    """Escape the LIKE metacharacters so a key is matched literally.

    Without this, `/forget 100%` or a key containing `_` behaves as a wildcard pattern, and a
    key that is a substring of many memories can be reported as "not found" simply because the
    ten-row cap filled up with unrelated matches.
    """

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def list_memories(session: AsyncSession, *, user_id: int, limit: int = 50) -> list[Memory]:
    result = await session.execute(
        sa.select(Memory)
        .where(Memory.user_id == user_id, Memory.deleted_at.is_(None))
        .order_by(Memory.key)
        .limit(limit)
    )
    return list(result.scalars())


async def search_memories(
    session: AsyncSession, *, user_id: int, query: str, limit: int = 10
) -> list[Memory]:
    """Lexical search over key and value.

    Deliberately not embeddings: memories are short, explicit, user-authored strings where a
    substring match is both sufficient and explainable (§6.2 defers hybrid search to a
    measured trigger for documents, not for this).
    """

    pattern = f"%{_escape_like(query.strip())}%"
    result = await session.execute(
        sa.select(Memory)
        .where(
            Memory.user_id == user_id,
            Memory.deleted_at.is_(None),
            sa.or_(
                Memory.key.ilike(pattern, escape="\\"),
                Memory.value.ilike(pattern, escape="\\"),
            ),
        )
        .order_by(Memory.key)
        .limit(limit)
    )
    return list(result.scalars())


async def get_memory(session: AsyncSession, *, user_id: int, memory_id: uuid.UUID) -> Memory | None:
    result = await session.execute(
        sa.select(Memory).where(
            Memory.id == memory_id, Memory.user_id == user_id, Memory.deleted_at.is_(None)
        )
    )
    return result.scalar_one_or_none()


async def get_memory_by_key(session: AsyncSession, *, user_id: int, key: str) -> Memory | None:
    result = await session.execute(
        sa.select(Memory).where(
            Memory.user_id == user_id, Memory.key == key, Memory.deleted_at.is_(None)
        )
    )
    return result.scalar_one_or_none()


async def delete_memory(session: AsyncSession, *, user_id: int, memory_id: uuid.UUID) -> bool:
    """Hard delete one memory. Returns True when a row was removed (idempotent otherwise)."""

    result = await session.execute(
        sa.delete(Memory).where(Memory.id == memory_id, Memory.user_id == user_id)
    )
    return bool(result.rowcount)


async def delete_memory_by_key(session: AsyncSession, *, user_id: int, key: str) -> int:
    result = await session.execute(
        sa.delete(Memory).where(Memory.user_id == user_id, Memory.key == key)
    )
    return int(result.rowcount or 0)


async def count_memories(session: AsyncSession, *, user_id: int) -> int:
    result = await session.execute(
        sa.select(sa.func.count()).select_from(Memory).where(
            Memory.user_id == user_id, Memory.deleted_at.is_(None)
        )
    )
    return int(result.scalar_one())


async def delete_all_memories(session: AsyncSession, *, user_id: int) -> int:
    result = await session.execute(sa.delete(Memory).where(Memory.user_id == user_id))
    return int(result.rowcount or 0)


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #
async def create_task(
    session: AsyncSession,
    *,
    user_id: int,
    title: str,
    description: str | None,
    due_at: datetime,
    timezone: str,
    idempotency_key: str,
    max_attempts: int,
) -> tuple[Task, bool]:
    """Create a reminder, or return the existing one for the same idempotency key.

    Returns ``(task, created)``. The unique index on ``(user_id, idempotency_key)`` is what
    makes a retried tool call resolve to the existing task instead of a duplicate reminder.
    """

    statement = (
        pg_insert(Task)
        .values(
            user_id=user_id,
            title=title,
            description=description,
            due_at=due_at,
            timezone=timezone,
            state=TASK_PENDING,
            next_notification_at=due_at,
            max_attempts=max_attempts,
            idempotency_key=idempotency_key,
        )
        .on_conflict_do_nothing(index_elements=[Task.user_id, Task.idempotency_key])
        .returning(Task)
    )
    created_row = (await session.execute(statement)).scalar_one_or_none()
    if created_row is not None:
        return created_row, True

    existing = await session.execute(
        sa.select(Task).where(Task.user_id == user_id, Task.idempotency_key == idempotency_key)
    )
    return existing.scalar_one(), False


async def list_tasks(
    session: AsyncSession, *, user_id: int, status: str | None = None, limit: int = 50
) -> list[Task]:
    statement = sa.select(Task).where(Task.user_id == user_id)
    if status is not None:
        statement = statement.where(Task.state == status)
    statement = statement.order_by(Task.due_at.asc()).limit(limit)
    return list((await session.execute(statement)).scalars())


async def get_task(session: AsyncSession, *, user_id: int, task_id: uuid.UUID) -> Task | None:
    result = await session.execute(
        sa.select(Task).where(Task.id == task_id, Task.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def set_task_state(
    session: AsyncSession, *, user_id: int, task_id: uuid.UUID, state: str
) -> Task | None:
    """Move a task to a terminal state, only from a non-terminal one.

    The predicate is in the statement, so a concurrent completion cannot be overwritten.
    """

    column = {
        TASK_COMPLETED: "completed_at",
        TASK_CANCELLED: "cancelled_at",
    }.get(state, "updated_at")
    values: dict[str, Any] = {"state": state, "updated_at": sa.func.now()}
    if column in {"completed_at", "cancelled_at"}:
        values[column] = sa.func.now()
    if state != TASK_PENDING:
        values["next_notification_at"] = None
        values["last_error"] = None

    result = await session.execute(
        sa.update(Task)
        .where(Task.id == task_id, Task.user_id == user_id, Task.state != state)
        .values(**values)
        .returning(Task)
    )
    updated = result.scalar_one_or_none()
    if updated is not None:
        return updated
    return await get_task(session, user_id=user_id, task_id=task_id)


# --------------------------------------------------------------------------- #
# Reminder dispatch (PLAN.md §5.2 — reconciliate, claim, ledger, advance)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ClaimedTask:
    task_id: uuid.UUID
    user_id: int
    title: str
    description: str | None
    due_at: datetime
    timezone: str
    occurrence_at: datetime
    attempts: int
    max_attempts: int


async def reconcile_stale_dispatches(
    session: AsyncSession, *, lease_seconds: int, executor: str
) -> list[tuple[uuid.UUID, datetime]]:
    """Phase 0: a `claimed` row whose lease expired becomes `unknown`. Never re-sent.

    Closes the crash window between the ledger insert and `sendMessage`: such a row used to
    stay `claimed` forever, the unique constraint blocked any later insert for that occurrence,
    and the reminder vanished without being reported as anything (B2).
    """

    result = await session.execute(
        sa.update(ReminderDispatch)
        .where(
            ReminderDispatch.state == DISPATCH_CLAIMED,
            # `make_interval(secs => :lease_seconds)` keeps the value a bound parameter; the
            # earlier `sa.text(f"interval '{int(lease_seconds)} seconds'")` was not injectable
            # (the value is a validated int) but it is exactly the string-built-SQL shape the
            # plan forbids, and §7.3 now has a guard that enforces it.
            ReminderDispatch.claimed_at
            < sa.func.now()
            - sa.func.make_interval(0, 0, 0, 0, 0, 0, sa.bindparam("lease_seconds", lease_seconds)),
        )
        .values(
            state=DISPATCH_UNKNOWN,
            reconciled_at=sa.func.now(),
            error="lease expired before send",
            claimed_by=executor,
        )
        .returning(ReminderDispatch.task_id, ReminderDispatch.occurrence_at)
    )
    return [(row[0], row[1]) for row in result.all()]


async def claim_due_tasks(
    session: AsyncSession, *, batch: int, executor: str
) -> list[ClaimedTask]:
    """Phase 1: claim a disjoint batch of due work with `FOR UPDATE SKIP LOCKED`.

    ``attempts < max_attempts`` is enforced here, in SQL, so a task that has exhausted its
    retries cannot be picked up again (B1). ``attempts`` is deliberately **not** incremented in
    this statement: an attempt is consumed only when the ledger insert actually returned a row
    (RN6), so contention between two dispatchers cannot burn a task's retries.
    """

    due = (
        sa.select(Task.id)
        .where(
            Task.state == TASK_PENDING,
            Task.next_notification_at.is_not(None),
            Task.next_notification_at <= sa.func.now(),
            Task.attempts < Task.max_attempts,
        )
        .order_by(Task.next_notification_at.asc())
        .limit(batch)
        .with_for_update(skip_locked=True)
        .subquery()
    )
    statement = (
        sa.update(Task)
        .where(Task.id.in_(sa.select(due.c.id)))
        .values(claimed_at=sa.func.now(), claimed_by=executor)
        .returning(
            Task.id,
            Task.user_id,
            Task.title,
            Task.description,
            Task.due_at,
            Task.timezone,
            Task.next_notification_at,
            Task.attempts,
            Task.max_attempts,
        )
    )
    rows = (await session.execute(statement)).all()
    return [
        ClaimedTask(
            task_id=row[0],
            user_id=row[1],
            title=row[2],
            description=row[3],
            due_at=row[4],
            timezone=row[5],
            occurrence_at=row[6],
            attempts=row[7],
            max_attempts=row[8],
        )
        for row in rows
    ]


async def claim_dispatch(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    occurrence_at: datetime,
    dispatch_key: str,
    executor: str,
) -> int | None:
    """Take the right to send one occurrence. `None` means somebody else already has it."""

    statement = (
        pg_insert(ReminderDispatch)
        .values(
            task_id=task_id,
            occurrence_at=occurrence_at,
            dispatch_key=dispatch_key,
            state=DISPATCH_CLAIMED,
            claimed_at=sa.func.now(),
            claimed_by=executor,
            attempts=1,
        )
        .on_conflict_do_nothing(
            index_elements=[ReminderDispatch.task_id, ReminderDispatch.occurrence_at]
        )
        .returning(ReminderDispatch.id)
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def increment_task_attempts(session: AsyncSession, *, task_id: uuid.UUID) -> None:
    """Consume one attempt — only after the ledger insert returned a row (RN6)."""

    await session.execute(
        sa.update(Task).where(Task.id == task_id).values(attempts=Task.attempts + 1)
    )


async def mark_dispatch_sent(
    session: AsyncSession, *, dispatch_id: int, telegram_message_id: int | None
) -> int:
    """Guarded CAS: returns the rowcount so a zero-row outcome can be diagnosed (RN5)."""

    result = await session.execute(
        sa.update(ReminderDispatch)
        .where(ReminderDispatch.id == dispatch_id, ReminderDispatch.state == DISPATCH_CLAIMED)
        .values(
            state=DISPATCH_SENT,
            telegram_message_id=telegram_message_id,
            sent_at=sa.func.now(),
            error=None,
        )
    )
    return int(result.rowcount or 0)


async def mark_dispatch_failed(session: AsyncSession, *, dispatch_id: int, error: str) -> int:
    result = await session.execute(
        sa.update(ReminderDispatch)
        .where(ReminderDispatch.id == dispatch_id, ReminderDispatch.state == DISPATCH_CLAIMED)
        .values(state=DISPATCH_FAILED, error=error)
    )
    return int(result.rowcount or 0)


async def advance_task_on_delivery(session: AsyncSession, *, task_id: uuid.UUID) -> None:
    """The success path: maintain `last_notified_at`, clear the next occurrence, complete.

    Same transaction as the ledger mark, so there is no window in which the send is recorded but
    the task still looks due (B1). The MVP has no recurrence rules, so a delivered one-shot task
    is finished by definition.
    """

    await session.execute(
        sa.update(Task)
        .where(Task.id == task_id)
        .values(
            last_notified_at=sa.func.now(),
            next_notification_at=None,
            state=TASK_COMPLETED,
            completed_at=sa.func.now(),
            last_error=None,
            updated_at=sa.func.now(),
        )
    )


async def advance_task_on_failure(
    session: AsyncSession, *, task_id: uuid.UUID, error: str, retry_at: datetime | None
) -> str:
    """Record a definite failure; exhaust the task only when the cap is reached."""

    result = await session.execute(
        sa.update(Task)
        .where(Task.id == task_id)
        .values(
            last_error=error[:500],
            next_notification_at=retry_at,
            state=sa.case((Task.attempts >= Task.max_attempts, TASK_FAILED), else_=TASK_PENDING),
            updated_at=sa.func.now(),
        )
        .returning(Task.state)
    )
    return str(result.scalar_one())


async def list_open_dispatches(session: AsyncSession, *, limit: int = 50) -> list[ReminderDispatch]:
    result = await session.execute(
        sa.select(ReminderDispatch)
        .where(ReminderDispatch.state.in_([DISPATCH_CLAIMED, DISPATCH_UNKNOWN]))
        .order_by(ReminderDispatch.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars())


# --------------------------------------------------------------------------- #
# Embeddings and chunks (the shape is frozen here; Phase 4 provides the behaviour)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ChunkMatch:
    """One retrieved chunk, carrying everything a citation needs."""

    chunk_id: int
    document_id: uuid.UUID
    chunk_index: int
    page_number: int | None
    text: str
    document_filename: str
    score: float


@dataclass(frozen=True, slots=True)
class ActiveEmbeddingModel:
    model_name: str
    dim: int
    pipeline_version: int


async def get_active_embedding_model(session: AsyncSession) -> ActiveEmbeddingModel | None:
    """Read the single authority for the active model (B7). Retrieval and /readyz use this."""

    row = (
        await session.execute(sa.select(EmbeddingModel).where(EmbeddingModel.is_active.is_(True)))
    ).scalar_one_or_none()
    if row is None:
        return None
    return ActiveEmbeddingModel(
        model_name=row.model_name, dim=row.dim, pipeline_version=row.pipeline_version
    )


async def upsert_embedding_model(
    session: AsyncSession,
    *,
    model_name: str,
    dim: int,
    pipeline_version: int,
    activate: bool,
) -> None:
    """Create-if-absent, then optionally make it the active model.

    Called by ingestion in the same transaction as its first chunk batch, so the active row is
    always the model the chunks were written with (RN13).
    """

    statement = (
        pg_insert(EmbeddingModel)
        .values(model_name=model_name, dim=dim, pipeline_version=pipeline_version, is_active=False)
        .on_conflict_do_update(
            index_elements=[EmbeddingModel.model_name],
            set_={"dim": dim, "pipeline_version": pipeline_version},
        )
    )
    await session.execute(statement)
    if activate:
        await session.execute(
            sa.update(EmbeddingModel)
            .where(EmbeddingModel.is_active.is_(True), EmbeddingModel.model_name != model_name)
            .values(is_active=False)
        )
        await session.execute(
            sa.update(EmbeddingModel)
            .where(EmbeddingModel.model_name == model_name)
            .values(is_active=True)
        )


async def search_chunks(
    session: AsyncSession,
    *,
    owner_user_id: int,
    query_embedding: Sequence[float],
    embed_model: str,
    top_k: int,
    document_ids: Sequence[uuid.UUID] | None = None,
) -> list[ChunkMatch]:
    """THE single vector search. **Frozen signature.**

    Three predicates are inside the SQL literal, not caller-supplied filters:

    * ``c.owner_user_id = :owner_user_id`` — isolation (spec §9 "filtrar por propietario antes
      de la búsqueda vectorial"), on the same row as the vector so no join can leak;
    * ``c.embed_model = :embed_model`` — only vectors from the active model's space are
      comparable (decision 9);
    * ``d.pipeline_version = c.pipeline_version`` — the **document's** current version, so a
      re-index never mixes versions *and* the document stays answerable from the previous index
      throughout its own re-index (RB3-follow-on). `ready => retrievable` is the invariant.

    There is deliberately **no** caller-supplied ``pipeline_version`` parameter. Phase 2's first
    version had one and never used it — the version has to come from the document
    (RB3-follow-on), so a caller passing "the active pipeline version" would have been silently
    ignored. A parameter that cannot do anything is a trap in a frozen contract; it was removed
    and the change is recorded in the phase report.

    ``SET LOCAL hnsw.iterative_scan = strict_order`` makes a *filtered* approximate search
    return exactly ``top_k`` rows instead of however many the index happened to visit
    (Appendix A.2/A.3); it is transaction-local, so it cannot leak onto a pooled connection.
    """

    await session.execute(sa.text("SET LOCAL hnsw.iterative_scan = strict_order"))

    distance = DocumentChunk.embedding.cosine_distance(list(query_embedding)).label("distance")
    statement = (
        sa.select(
            DocumentChunk.id,
            DocumentChunk.document_id,
            DocumentChunk.chunk_index,
            DocumentChunk.page_number,
            DocumentChunk.text,
            # `original_filename`, because this value becomes a citation in Phase 4 and the user
            # must recognise their own document (§3.2 marks original_filename as the display
            # value; sanitized_filename is the storage name).
            Document.original_filename,
            distance,
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            DocumentChunk.owner_user_id == owner_user_id,
            DocumentChunk.embed_model == embed_model,
            Document.pipeline_version == DocumentChunk.pipeline_version,
            Document.ingest_status == "ready",
            Document.deleted_at.is_(None),
        )
        .order_by(distance)
        .limit(top_k)
    )
    if document_ids is not None:
        statement = statement.where(DocumentChunk.document_id.in_(list(document_ids)))

    rows = (await session.execute(statement)).all()
    return [
        ChunkMatch(
            chunk_id=row[0],
            document_id=row[1],
            chunk_index=row[2],
            page_number=row[3],
            text=row[4],
            document_filename=row[5],
            score=1.0 - float(row[6]),
        )
        for row in rows
    ]



# --------------------------------------------------------------------------- #
# Phase 3: approvals, the tool ledger and the resume queue
#
# All SQL stays in this one module (PLAN.md §2.1, §15): the statements below are the §4.2
# CAS, the §4.5 diagnostic read, the §5.3 ledger and the §5.5 watchdog queue.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ClaimedAction:
    """What a successful claim returns to the executor."""

    id: UUID
    action_id: str
    action_type: str
    args_json: dict[str, Any]
    args_canonical: str
    args_hash: str
    args_revision: int
    idempotency_key: str
    user_id: int
    chat_id: int
    attempts: int


async def insert_action(
    session: AsyncSession,
    *,
    action_id: str,
    user_id: int,
    chat_id: int,
    action_type: str,
    args_json: dict[str, Any],
    args_hash: str,
    args_revision: int,
    idempotency_key: str,
    preview_text: str,
    expires_at: datetime,
) -> UUID | None:
    """Create a pending action, or return `None` if an identical live one already exists.

    The partial unique index on `(user_id, action_type, args_hash) WHERE state IN
    ('pending','approved','executing')` is the arbiter: the model cannot stack three identical
    approval prompts.
    """

    statement = (
        sa.text(
            """
            INSERT INTO pending_actions
                (action_id, user_id, chat_id, action_type, args_json, args_canonical, args_hash,
                 args_revision, state, idempotency_key, preview_text, expires_at)
            VALUES
                (:action_id, :user_id, :chat_id, :action_type, CAST(:args_json AS jsonb),
                 :args_canonical, :args_hash, :args_revision, 'pending', :idempotency_key,
                 :preview_text, :expires_at)
            ON CONFLICT (user_id, action_type, args_hash)
                WHERE state IN ('pending', 'approved', 'executing')
            DO NOTHING
            RETURNING id
            """
        ).bindparams(
            action_id=action_id,
            user_id=user_id,
            chat_id=chat_id,
            action_type=action_type,
            args_json=canonical_args(args_json),
            args_canonical=canonical_args(args_json),
            args_hash=args_hash,
            args_revision=args_revision,
            idempotency_key=idempotency_key,
            preview_text=preview_text,
            expires_at=expires_at,
        )
    )
    # `args_json` reaches the column as the canonical serialisation, so the value inserted is
    # exactly the value the hash was computed from (sorted keys, no insignificant whitespace);
    # `CAST(... AS jsonb)` in the statement pins the type without a nested bind parameter.
    return (await session.execute(statement)).scalar_one_or_none()


async def decide_action(
    session: AsyncSession,
    *,
    action_id: str,
    user_id: int,
    chat_id: int,
    args_revision: int,
    approve: bool,
) -> UUID | None:
    """Step 1: the guarded decision CAS. Zero rows means "diagnose, do not guess"."""

    target = "approved" if approve else "rejected"
    statement = (
        sa.text(
            """
            UPDATE pending_actions
               SET state = :target, decided_at = now(), decided_by = :user_id
             WHERE action_id     = :action_id
               AND state         = 'pending'
               AND user_id       = :user_id
               AND chat_id       = :chat_id
               AND args_revision = :args_revision
               AND expires_at    > now()
            RETURNING id
            """
        ).bindparams(
            target=target,
            action_id=action_id,
            user_id=user_id,
            chat_id=chat_id,
            args_revision=args_revision,
        )
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def claim_action(
    session: AsyncSession,
    *,
    action_id: str,
    user_id: int,
    chat_id: int,
    args_revision: int,
    args_hash_prefix: str,
    executor: str,
) -> ClaimedAction | None:
    """Step 2: the consumption CAS.

    All five of spec §7's re-checks are in this one statement — `estado`, `usuario`, `chat`,
    `argumentos` (revision *and* hash) and `caducidad` — so the mutual exclusion is the row lock
    and there is no read-then-write window. The hash is compared against the row's **prefix**,
    because that is all `callback_data` can carry (N1/RN9).
    """

    statement = (
        sa.text(
            """
            UPDATE pending_actions
               SET state = 'executing', claimed_at = now(), claimed_by = :executor,
                   attempts = attempts + 1, executed_at = now()
             WHERE action_id     = :action_id
               AND state         = 'approved'
               AND user_id       = :user_id
               AND chat_id       = :chat_id
               AND args_revision = :args_revision
               AND left(args_hash, :prefix_len) = :args_hash_prefix
               AND expires_at    > now()
            RETURNING id, action_id, action_type, args_json, args_canonical, args_hash,
                      args_revision, idempotency_key, user_id, chat_id, attempts
            """
        ).bindparams(
            action_id=action_id,
            user_id=user_id,
            chat_id=chat_id,
            args_revision=args_revision,
            prefix_len=len(args_hash_prefix),
            args_hash_prefix=args_hash_prefix,
            executor=executor,
        )
    )
    row = (await session.execute(statement)).first()
    if row is None:
        return None
    return ClaimedAction(
        id=row[0],
        action_id=row[1],
        action_type=row[2],
        args_json=row[3],
        args_canonical=row[4],
        args_hash=row[5],
        args_revision=row[6],
        idempotency_key=row[7],
        user_id=row[8],
        chat_id=row[9],
        attempts=row[10],
    )


async def diagnose_action(session: AsyncSession, *, action_id: str) -> RowView | None:
    """The §4.5 diagnostic read: `SELECT` only, so it cannot change the outcome."""

    row = (
        await session.execute(
            sa.text(
                """
                SELECT state, args_revision, args_hash, expires_at, user_id, chat_id, decided_by
                  FROM pending_actions WHERE action_id = :action_id
                """
            ).bindparams(action_id=action_id)
        )
    ).first()
    if row is None:
        return None
    return RowView(
        state=row[0],
        args_revision=row[1],
        args_hash=row[2],
        expires_at=row[3],
        user_id=row[4],
        chat_id=row[5],
        decided_by=row[6],
    )


async def finish_action(
    session: AsyncSession,
    *,
    action_id: str,
    state: str,
    result_json: dict[str, Any] | None = None,
    error: str | None = None,
) -> int:
    """CAS `executing -> succeeded|failed|unknown`, keyed on the state being `executing`.

    A stale executor therefore cannot overwrite a reconciled outcome.
    """

    statement = sa.text(
        """
        UPDATE pending_actions
           SET state = :state,
               result_json = CAST(:result_json AS jsonb),
               error = :error
         WHERE action_id = :action_id AND state = 'executing'
        """
    ).bindparams(
        state=state,
        action_id=action_id,
        result_json=canonical_args(result_json) if result_json is not None else None,
        error=error,
    )
    return int((await session.execute(statement)).rowcount or 0)


async def expire_lapsed_actions(session: AsyncSession) -> int:
    """Bulk expiry. Bookkeeping only: the claim's own `expires_at` guard is the correctness."""

    result = await session.execute(
        sa.text(
            """
            UPDATE pending_actions SET state = 'expired'
             WHERE state IN ('pending', 'approved') AND expires_at <= now()
            """
        )
    )
    return int(result.rowcount or 0)


async def get_action(session: AsyncSession, *, action_id: str) -> dict[str, Any] | None:
    row = (
        await session.execute(
            sa.text(
                """
                SELECT id, action_id, action_type, args_json, args_hash, args_revision, state,
                       idempotency_key, preview_text, user_id, chat_id, expires_at,
                       telegram_message_id
                  FROM pending_actions WHERE action_id = :action_id
                """
            ).bindparams(action_id=action_id)
        )
    ).mappings().first()
    return None if row is None else dict(row)


async def set_action_message_id(
    session: AsyncSession, *, action_id: str, message_id: int
) -> None:
    await session.execute(
        sa.text(
            "UPDATE pending_actions SET telegram_message_id = :message_id WHERE action_id = :action_id"
        ).bindparams(message_id=message_id, action_id=action_id)
    )


async def count_actions_for_key_prefix(
    session: AsyncSession, *, idempotency_prefix: str
) -> int:
    """How many rows already exist for a logical action, in any state.

    Used to fold the attempt number into the idempotency key: the column is UNIQUE (spec §3.2),
    and a *terminal* row keeps its key occupied, so a re-request of the same action must be a
    distinct row rather than a violation of the full constraint (§4.1: "a retry is a new row").
    """

    result = await session.execute(
        sa.text(
            "SELECT count(*) FROM pending_actions WHERE idempotency_key LIKE :prefix"
        ).bindparams(prefix=f"{idempotency_prefix}#%")
    )
    return int(result.scalar_one())


async def count_actions(session: AsyncSession, *, user_id: int) -> int:
    result = await session.execute(
        sa.text("SELECT count(*) FROM pending_actions WHERE user_id = :user_id").bindparams(
            user_id=user_id
        )
    )
    return int(result.scalar_one())


# --------------------------------------------------------------------------- #
# Tool invocation ledger (§5.3)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class InvocationRow:
    id: int
    state: str
    args_hash: str
    result_json: dict[str, Any] | None
    started_at: datetime


async def find_invocation(
    session: AsyncSession, *, thread_id: str, run_key: str, attempt_no: int
) -> InvocationRow | None:
    row = (
        await session.execute(
            sa.text(
                """
                SELECT id, state, args_hash, result_json, started_at
                  FROM tool_invocations
                 WHERE thread_id = :thread_id AND run_key = :run_key AND attempt_no = :attempt_no
                """
            ).bindparams(thread_id=thread_id, run_key=run_key, attempt_no=attempt_no)
        )
    ).first()
    if row is None:
        return None
    return InvocationRow(
        id=row[0], state=row[1], args_hash=row[2], result_json=row[3], started_at=row[4]
    )


async def start_invocation(
    session: AsyncSession,
    *,
    thread_id: str,
    run_key: str,
    attempt_no: int,
    tool_name: str,
    tool_call_id: str | None,
    args_hash: str,
) -> int:
    """Insert the `started` row. Committed before the side effect (spec §5.3)."""

    statement = (
        sa.text(
            """
            INSERT INTO tool_invocations
                (thread_id, run_key, attempt_no, tool_call_id, tool_name, args_hash, state)
            VALUES (:thread_id, :run_key, :attempt_no, :tool_call_id, :tool_name, :args_hash,
                    'started')
            ON CONFLICT (thread_id, run_key, attempt_no) DO NOTHING
            RETURNING id
            """
        ).bindparams(
            thread_id=thread_id,
            run_key=run_key,
            attempt_no=attempt_no,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            args_hash=args_hash,
        )
    )
    return int((await session.execute(statement)).scalar_one_or_none() or 0)


async def finish_invocation(
    session: AsyncSession,
    *,
    thread_id: str,
    run_key: str,
    attempt_no: int,
    state: str,
    result_json: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    await session.execute(
        sa.text(
            """
            UPDATE tool_invocations
               SET state = :state, result_json = CAST(:result_json AS jsonb), error = :error,
                   finished_at = now()
             WHERE thread_id = :thread_id AND run_key = :run_key AND attempt_no = :attempt_no
            """
        ).bindparams(
            state=state,
            thread_id=thread_id,
            run_key=run_key,
            attempt_no=attempt_no,
            result_json=canonical_args(result_json) if result_json is not None else None,
            error=error,
        )
    )


async def list_invocations(session: AsyncSession, *, thread_id: str) -> list[InvocationRow]:
    rows = (
        await session.execute(
            sa.text(
                """
                SELECT id, state, args_hash, result_json, started_at
                  FROM tool_invocations WHERE thread_id = :thread_id ORDER BY id
                """
            ).bindparams(thread_id=thread_id)
        )
    ).all()
    return [
        InvocationRow(
            id=row[0], state=row[1], args_hash=row[2], result_json=row[3], started_at=row[4]
        )
        for row in rows
    ]


# --------------------------------------------------------------------------- #
# The resume queue (§5.5)
# --------------------------------------------------------------------------- #
async def enqueue_work_item(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    thread_id: str,
    kind: str,
    run_at: datetime,
    payload: dict[str, Any] | None = None,
) -> int | None:
    """The watchdog enqueue: inserted **before** the graph runs, marked `done` after the reply.

    `ON CONFLICT DO NOTHING` on the partial unique index is the second-turn rule: with a turn in
    flight the row is already `pending`, so a second message creates nothing and the handler
    declines it (RB1-follow-on).
    """

    statement = sa.text(
        """
        INSERT INTO work_items (kind, conversation_id, thread_id, payload, run_at, state)
        VALUES (:kind, :conversation_id, :thread_id, CAST(:payload AS jsonb), :run_at, 'pending')
        ON CONFLICT (thread_id, kind) WHERE state IN ('pending', 'claimed')
        DO NOTHING
        RETURNING id
        """
    ).bindparams(
        kind=kind,
        conversation_id=conversation_id,
        thread_id=thread_id,
        payload=canonical_args(payload or {}),
        run_at=run_at,
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def mark_work_item_done(
    session: AsyncSession, *, thread_id: str, kind: str, state: str = "done"
) -> int:
    result = await session.execute(
        sa.text(
            """
            UPDATE work_items SET state = :state, finished_at = now()
             WHERE thread_id = :thread_id AND kind = :kind AND state IN ('pending', 'claimed')
            """
        ).bindparams(state=state, thread_id=thread_id, kind=kind)
    )
    return int(result.rowcount or 0)


async def claim_due_work_items(
    session: AsyncSession, *, batch: int, executor: str, lease_seconds: int
) -> list[dict[str, Any]]:
    """Claim resume work with a lease, so one run owns a thread at a time."""

    statement = sa.text(
        """
        WITH due AS (
            SELECT id FROM work_items
             WHERE state = 'pending' AND run_at <= now() AND attempts < max_attempts
             ORDER BY run_at LIMIT :batch
             FOR UPDATE SKIP LOCKED
        )
        UPDATE work_items w
           SET state = 'claimed', claimed_at = now(), claimed_by = :executor,
               attempts = w.attempts + 1,
               lease_expires_at = now() + make_interval(0, 0, 0, 0, 0, 0, :lease_seconds)
          FROM due
         WHERE w.id = due.id
        RETURNING w.id, w.thread_id, w.conversation_id, w.kind, w.payload, w.attempts,
                  w.max_attempts
        """
    ).bindparams(batch=batch, executor=executor, lease_seconds=lease_seconds)
    return [dict(row) for row in (await session.execute(statement)).mappings().all()]


async def finish_work_item(
    session: AsyncSession, *, item_id: int, state: str, error: str | None = None
) -> int:
    result = await session.execute(
        sa.text(
            """
            UPDATE work_items SET state = :state, finished_at = now(), last_error = :error
             WHERE id = :item_id AND state = 'claimed'
            """
        ).bindparams(state=state, item_id=item_id, error=error)
    )
    return int(result.rowcount or 0)


async def work_item_state(session: AsyncSession, *, thread_id: str, kind: str) -> str | None:
    result = await session.execute(
        sa.text(
            "SELECT state FROM work_items WHERE thread_id = :thread_id AND kind = :kind "
            "ORDER BY id DESC LIMIT 1"
        ).bindparams(thread_id=thread_id, kind=kind)
    )
    return result.scalar_one_or_none()


async def count_work_items(session: AsyncSession, *, thread_id: str | None = None) -> int:
    if thread_id is None:
        result = await session.execute(sa.text("SELECT count(*) FROM work_items"))
    else:
        result = await session.execute(
            sa.text("SELECT count(*) FROM work_items WHERE thread_id = :thread_id").bindparams(
                thread_id=thread_id
            )
        )
    return int(result.scalar_one())


async def active_turn_items(
    session: AsyncSession, *, thread_id: str | None = None
) -> Sequence[dict[str, Any]]:
    """Rows that still look like an in-flight turn (for the orphan check of (h))."""

    query = (
        "SELECT id, thread_id, kind, state, created_at, finished_at FROM work_items "
        "WHERE state IN ('pending', 'claimed', 'failed', 'dead')"
    )
    if thread_id is not None:
        query += " AND thread_id = :thread_id"
    statement = sa.text(query)
    if thread_id is not None:
        statement = statement.bindparams(thread_id=thread_id)
    return [dict(row) for row in (await session.execute(statement)).mappings().all()]

__all__ = [
    "count_actions_for_key_prefix",
    "work_item_state",
    "start_invocation",
    "set_action_message_id",
    "mark_work_item_done",
    "list_invocations",
    "insert_action",
    "get_action",
    "finish_work_item",
    "finish_invocation",
    "finish_action",
    "find_invocation",
    "expire_lapsed_actions",
    "enqueue_work_item",
    "diagnose_action",
    "decide_action",
    "count_work_items",
    "count_actions",
    "claim_due_work_items",
    "claim_action",
    "active_turn_items",
    "InvocationRow",
    "ClaimedAction",
    "ActiveEmbeddingModel",
    "ChunkMatch",
    "ClaimedTask",
    "DISPATCH_CLAIMED",
    "DISPATCH_FAILED",
    "DISPATCH_SENT",
    "DISPATCH_UNKNOWN",
    "INBOX_FAILED",
    "INBOX_IGNORED",
    "INBOX_NOTICED",
    "INBOX_PROCESSED",
    "INBOX_RECEIVED",
    "TASK_CANCELLED",
    "TASK_COMPLETED",
    "TASK_FAILED",
    "TASK_PENDING",
    "_escape_like",
    "advance_task_on_delivery",
    "advance_task_on_failure",
    "append_message",
    "build_thread_id",
    "claim_dispatch",
    "claim_due_tasks",
    "claim_inbox_update",
    "count_memories",
    "create_task",
    "delete_all_memories",
    "delete_memory",
    "delete_memory_by_key",
    "get_active_conversation",
    "get_active_embedding_model",
    "get_inbox_update",
    "get_memory",
    "get_memory_by_key",
    "get_or_create_active_conversation",
    "get_task",
    "increment_task_attempts",
    "list_memories",
    "list_messages",
    "list_open_dispatches",
    "list_tasks",
    "mark_dispatch_failed",
    "mark_dispatch_sent",
    "mark_inbox_update",
    "reconcile_stale_dispatches",
    "rotate_conversation_context",
    "search_chunks",
    "search_memories",
    "set_task_state",
    "touch_conversation",
    "upsert_embedding_model",
    "upsert_memory",
]
