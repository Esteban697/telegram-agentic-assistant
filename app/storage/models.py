"""SQLAlchemy ORM models — the canonical schema (PLAN.md §3.2).

Layer 1 (``storage/models.py``): imports only layer 0. It exists separately from
``repositories.py`` so that ``migrations/env.py`` can import the metadata for autogenerate
without importing the data-access layer.

Conventions (§3.1):
* ``uuid`` primary keys for externally referenced entities, ``bigserial`` for append-only ledgers;
* every table carries ``created_at``; mutable tables carry ``updated_at``;
* all timestamps are ``timestamptz``;
* ids that enforce an idempotency guarantee are real constraints, listed in §3.3.

The tables for Phases 3 and 4 are defined here too, and created by the migration, because
§3.4 specifies a single ordered revision list and acceptance (a) requires every table plus the
HNSW index to exist after ``alembic upgrade head`` on an empty database. Their *behaviour*
arrives with their phase; their *shape* must not change later.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Stable constraint names keep Alembic's autogenerate diffs readable and comparable.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

EMBEDDING_DIM = 768

# Status vocabularies, mirroring the CHECK constraints and the Phase 1 in-memory ledger.
INBOX_STATUSES = ("received", "processed", "ignored", "failed", "noticed")
INGEST_STATUSES = ("pending", "processing", "ready", "failed")
DELETE_STATES = ("none", "requested", "chunks_deleted", "object_deleted", "complete", "failed")
TASK_STATES = ("pending", "completed", "cancelled", "failed")
DISPATCH_STATES = ("claimed", "sent", "failed", "unknown")
ACTION_STATES = (
    "pending",
    "approved",
    "rejected",
    "expired",
    "executing",
    "succeeded",
    "failed",
    "unknown",
)


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))


def _updated_at() -> Mapped[datetime]:
    return mapped_column(sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))


def _json() -> Mapped[dict[str, Any]]:
    return mapped_column(postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))


# --------------------------------------------------------------------------- #
# Conversations and the inbound ledger
# --------------------------------------------------------------------------- #
class Conversation(Base):
    """One Telegram chat's active conversation and the LangGraph thread behind it."""

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    telegram_user_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    telegram_chat_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    thread_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    context_revision: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default=sa.text("'active'"))
    last_activity_at: Mapped[datetime] = _created_at()
    meta: Mapped[dict[str, Any]] = _json()
    created_at: Mapped[datetime] = _created_at()

    messages: Mapped[list["Message"]] = relationship(back_populates="conversation")

    __table_args__ = (
        sa.UniqueConstraint("thread_id", name="uq_conversations_thread_id"),
        sa.UniqueConstraint(
            "telegram_user_id",
            "telegram_chat_id",
            "context_revision",
            name="uq_conversations_user_chat_revision",
        ),
        sa.CheckConstraint("status IN ('active', 'reset')", name="status_values"),
        sa.Index(
            "uq_conversations_active_chat",
            "telegram_chat_id",
            unique=True,
            postgresql_where=sa.text("status = 'active'"),
        ),
        sa.Index("ix_conversations_user_activity", "telegram_user_id", "last_activity_at"),
    )


class Message(Base):
    """Append-only transcript. It is **never** read back as conversational history.

    Decision 3: the LangGraph checkpoints are the single source of conversational truth; this
    table is the audit log (and the place ``telegram_update_id`` is made unique, spec §8).
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    telegram_update_id: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    role: Mapped[str] = mapped_column(sa.Text, nullable=False)
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    content_retained: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("true"))
    meta: Mapped[dict[str, Any]] = _json()
    created_at: Mapped[datetime] = _created_at()

    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    __table_args__ = (
        sa.CheckConstraint("role IN ('user', 'assistant', 'system', 'tool')", name="role_values"),
        sa.Index(
            "uq_messages_telegram_update_id",
            "telegram_update_id",
            unique=True,
            postgresql_where=sa.text("telegram_update_id IS NOT NULL"),
        ),
        sa.Index("ix_messages_conversation_created", "conversation_id", "created_at"),
    )


class InboxUpdate(Base):
    """The inbound update ledger. ``update_id`` as the primary key is *the* idempotency
    mechanism for Telegram delivery (PLAN.md §5.1)."""

    __tablename__ = "inbox_updates"

    update_id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    received_at: Mapped[datetime] = _created_at()
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default=sa.text("'received'"))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    payload_digest: Mapped[str | None] = mapped_column(sa.CHAR(64), nullable=True)

    __table_args__ = (
        sa.CheckConstraint(
            "kind IN ('message', 'edited_message', 'callback_query', 'unsupported', 'invalid')",
            name="kind_values",
        ),
        sa.CheckConstraint(
            "status IN ('received', 'processed', 'ignored', 'failed', 'noticed')",
            name="status_values",
        ),
        sa.Index("ix_inbox_updates_status", "status"),
    )


# --------------------------------------------------------------------------- #
# Explicit memory
# --------------------------------------------------------------------------- #
class Memory(Base):
    """An explicit or user-confirmed preference/fact (spec §8, §10)."""

    __tablename__ = "memories"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    value: Mapped[str] = mapped_column(sa.Text, nullable=False)
    memory_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source: Mapped[str] = mapped_column(sa.Text, nullable=False)
    confidence: Mapped[float] = mapped_column(sa.Numeric(3, 2), nullable=False, server_default=sa.text("1.00"))
    is_explicit: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("true"))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    deleted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    __table_args__ = (
        sa.CheckConstraint("length(key) BETWEEN 1 AND 128", name="key_length"),
        sa.CheckConstraint("length(value) <= 4000", name="value_length"),
        sa.CheckConstraint("memory_type IN ('preference', 'fact')", name="type_values"),
        sa.CheckConstraint("source IN ('explicit_user', 'confirmed')", name="source_values"),
        # Upsert semantics: one live value per key (PLAN.md §3.2).
        sa.Index(
            "uq_memories_user_key",
            "user_id",
            "key",
            unique=True,
            postgresql_where=sa.text("deleted_at IS NULL"),
        ),
        sa.Index("ix_memories_user", "user_id", postgresql_where=sa.text("deleted_at IS NULL")),
    )


# --------------------------------------------------------------------------- #
# Documents and the vector index (Phase 4 behaviour, Phase 2 schema)
# --------------------------------------------------------------------------- #
class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    original_filename: Mapped[str] = mapped_column(sa.Text, nullable=False)
    sanitized_filename: Mapped[str] = mapped_column(sa.Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    gcs_uri: Mapped[str] = mapped_column(sa.Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(sa.CHAR(64), nullable=False)
    page_count: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    ingest_status: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default=sa.text("'pending'"))
    ingest_error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    pipeline_version: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))
    chunk_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    deleted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    delete_state: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default=sa.text("'none'"))
    delete_error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    claimed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    chunks: Mapped[list["DocumentChunk"]] = relationship(back_populates="document")

    __table_args__ = (
        sa.CheckConstraint("size_bytes > 0", name="size_positive"),
        sa.CheckConstraint(
            "ingest_status IN ('pending', 'processing', 'ready', 'failed')", name="ingest_status_values"
        ),
        sa.CheckConstraint(
            "delete_state IN ('none', 'requested', 'chunks_deleted', 'object_deleted', 'complete', 'failed')",
            name="delete_state_values",
        ),
        # Ingestion idempotency by content hash and pipeline version (PLAN.md §3.3).
        sa.Index(
            "uq_documents_user_hash_version",
            "user_id",
            "content_sha256",
            "pipeline_version",
            unique=True,
            postgresql_where=sa.text("deleted_at IS NULL"),
        ),
        sa.Index("ix_documents_user_status", "user_id", "ingest_status"),
    )


class DocumentChunk(Base):
    """A page-scoped chunk with its embedding.

    ``owner_user_id`` is denormalised on purpose (decision 10): the isolation predicate must be
    on the same row as the vector, so the search needs no join to filter.
    """

    __tablename__ = "document_chunks"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    owner_user_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    chunk_index: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    page_number: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    char_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    embed_model: Mapped[str] = mapped_column(sa.Text, nullable=False)
    pipeline_version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    created_at: Mapped[datetime] = _created_at()

    document: Mapped[Document] = relationship(back_populates="chunks")

    __table_args__ = (
        sa.CheckConstraint("chunk_index >= 0", name="chunk_index_non_negative"),
        sa.CheckConstraint("page_number IS NULL OR page_number >= 1", name="page_number_positive"),
        # Chunk idempotency **per version** (RB3-follow-on): a retry inside one version
        # converges, while the previous version's rows survive under the same document_id.
        sa.UniqueConstraint(
            "document_id", "pipeline_version", "chunk_index", name="uq_document_chunks_version_chunk"
        ),
        sa.Index("ix_document_chunks_owner", "owner_user_id"),
        sa.Index("ix_document_chunks_document", "document_id"),
        sa.Index("ix_document_chunks_embed_model", "embed_model"),
        # HNSW, not IVFFlat (decision 6): buildable on an empty table, so it can live in the
        # migration, and `hnsw.iterative_scan` is what makes a filtered top-k exact.
        sa.Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class EmbeddingModel(Base):
    """The **single authority** for the active embedding model (B7)."""

    __tablename__ = "embedding_models"

    model_name: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    dim: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    pipeline_version: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("false"))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        sa.CheckConstraint("dim > 0", name="dim_positive"),
        sa.Index("uq_embedding_models_active", "is_active", unique=True, postgresql_where=sa.text("is_active")),
    )


# --------------------------------------------------------------------------- #
# Tasks and the reminder dispatch ledger
# --------------------------------------------------------------------------- #
class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    due_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(sa.Text, nullable=False)
    state: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default=sa.text("'pending'"))
    # NULL means "no further occurrence scheduled"; the partial index and the claim predicate
    # both exclude it naturally (B1).
    next_notification_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    last_notified_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    max_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("5"))
    last_error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    __table_args__ = (
        sa.CheckConstraint("length(title) BETWEEN 1 AND 200", name="title_length"),
        sa.CheckConstraint("state IN ('pending', 'completed', 'cancelled', 'failed')", name="state_values"),
        sa.CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        sa.Index(
            "ix_tasks_due",
            "next_notification_at",
            postgresql_where=sa.text("state = 'pending'"),
        ),
        # Declared as a unique *index* to match migration 0005, which creates an index.
        # A UniqueConstraint here and an index there made `alembic revision --autogenerate`
        # emit a spurious drop/add pair on every run (found by the Phase 2 review); the two
        # spellings are equivalent for `ON CONFLICT (user_id, idempotency_key)`.
        sa.Index("uq_tasks_user_idempotency_key", "user_id", "idempotency_key", unique=True),
    )


class ReminderDispatch(Base):
    """The dispatch ledger: THE exactly-once constraint for one reminder occurrence."""

    __tablename__ = "reminder_dispatches"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    occurrence_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    dispatch_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    state: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default=sa.text("'claimed'"))
    claimed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    reconciled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        sa.CheckConstraint("state IN ('claimed', 'sent', 'failed', 'unknown')", name="state_values"),
        sa.UniqueConstraint("task_id", "occurrence_at", name="uq_reminder_dispatches_occurrence"),
        sa.UniqueConstraint("dispatch_key", name="uq_reminder_dispatches_key"),
        sa.Index(
            "ix_reminder_dispatches_open",
            "state",
            postgresql_where=sa.text("state IN ('claimed', 'unknown')"),
        ),
    )


# --------------------------------------------------------------------------- #
# Approvals and resumption (Phase 3 behaviour, Phase 2 schema)
# --------------------------------------------------------------------------- #
class PendingAction(Base):
    __tablename__ = "pending_actions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    action_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    user_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    action_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    args_json: Mapped[dict[str, Any]] = mapped_column(postgresql.JSONB, nullable=False)
    # The canonical serialisation of the approved arguments, hashed by `args_hash`. It is stored
    # as *text* because jsonb does not round-trip floats faithfully (`1e20` comes back as an int),
    # which made the stale-hash guard refuse legitimate float arguments (migration 0007).
    args_canonical: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default=sa.text("''"))
    args_hash: Mapped[str] = mapped_column(sa.CHAR(64), nullable=False)
    args_revision: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))
    state: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default=sa.text("'pending'"))
    idempotency_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    preview_text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    decided_by: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(postgresql.JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    retry_of: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), sa.ForeignKey("pending_actions.id"), nullable=True
    )
    telegram_message_id: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)

    __table_args__ = (
        sa.CheckConstraint(
            "state IN ('pending', 'approved', 'rejected', 'expired', 'executing', 'succeeded', "
            "'failed', 'unknown')",
            name="state_values",
        ),
        sa.CheckConstraint("expires_at > created_at", name="expires_after_creation"),
        sa.UniqueConstraint("action_id", name="uq_pending_actions_action_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_pending_actions_idempotency_key"),
        sa.Index(
            "uq_pending_actions_live",
            "user_id",
            "action_type",
            "args_hash",
            unique=True,
            postgresql_where=sa.text("state IN ('pending', 'approved', 'executing')"),
        ),
        sa.Index(
            "ix_pending_actions_live_expiry",
            "state",
            "expires_at",
            postgresql_where=sa.text("state IN ('pending', 'approved')"),
        ),
        sa.Index(
            "ix_pending_actions_executing",
            "state",
            "claimed_at",
            postgresql_where=sa.text("state IN ('executing', 'unknown')"),
        ),
    )


class ToolInvocation(Base):
    """The re-execution guard that makes a resumed graph safe (PLAN.md §5.3)."""

    __tablename__ = "tool_invocations"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    run_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    attempt_no: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))
    tool_call_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    tool_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # The canonical serialisation of the approved arguments, hashed by `args_hash`. It is stored
    # as *text* because jsonb does not round-trip floats faithfully (`1e20` comes back as an int),
    # which made the stale-hash guard refuse legitimate float arguments (migration 0007).
    args_hash: Mapped[str] = mapped_column(sa.CHAR(64), nullable=False)
    state: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default=sa.text("'started'"))
    result_json: Mapped[dict[str, Any] | None] = mapped_column(postgresql.JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    started_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    __table_args__ = (
        sa.CheckConstraint("state IN ('started', 'succeeded', 'failed', 'unknown')", name="state_values"),
        sa.UniqueConstraint("thread_id", "run_key", "attempt_no", name="uq_tool_invocations_run"),
    )


class WorkItem(Base):
    """The durable resume queue (decision 2), enqueued by the watchdog before the graph runs."""

    __tablename__ = "work_items"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=False
    )
    thread_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload: Mapped[dict[str, Any]] = _json()
    run_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default=sa.text("'pending'"))
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    max_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("5"))
    claimed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    __table_args__ = (
        sa.CheckConstraint("kind IN ('resume_thread', 'deliver_reply')", name="kind_values"),
        sa.CheckConstraint(
            "state IN ('pending', 'claimed', 'done', 'failed', 'dead')", name="state_values"
        ),
        sa.Index("ix_work_items_due", "run_at", postgresql_where=sa.text("state = 'pending'")),
        sa.Index(
            "uq_work_items_open",
            "thread_id",
            "kind",
            unique=True,
            postgresql_where=sa.text("state IN ('pending', 'claimed')"),
        ),
    )


ALL_TABLES = (
    Conversation,
    Message,
    InboxUpdate,
    Memory,
    Document,
    DocumentChunk,
    EmbeddingModel,
    Task,
    ReminderDispatch,
    PendingAction,
    ToolInvocation,
    WorkItem,
)

__all__ = [
    "ACTION_STATES",
    "ALL_TABLES",
    "Base",
    "Conversation",
    "DELETE_STATES",
    "DISPATCH_STATES",
    "Document",
    "DocumentChunk",
    "EMBEDDING_DIM",
    "EmbeddingModel",
    "INGEST_STATUSES",
    "INBOX_STATUSES",
    "InboxUpdate",
    "Memory",
    "Message",
    "PendingAction",
    "ReminderDispatch",
    "TASK_STATES",
    "Task",
    "ToolInvocation",
    "WorkItem",
]
