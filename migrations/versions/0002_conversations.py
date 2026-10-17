"""Conversations, the message transcript and the inbound update ledger.

Revision ID: 0002_conversations
Revises: 0001_extensions
Create Date: 2026-10-17

`inbox_updates.update_id` as the primary key is *the* inbound idempotency constraint
(PLAN.md §3.3, §5.1): the webhook's first write is an `ON CONFLICT DO NOTHING` insert, so a
Telegram retry is harmless.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_conversations"
down_revision: str | None = "0001_extensions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("context_revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("meta", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('active', 'reset')", name="ck_conversations_status_values"),
        sa.UniqueConstraint("thread_id", name="uq_conversations_thread_id"),
        sa.UniqueConstraint(
            "telegram_user_id",
            "telegram_chat_id",
            "context_revision",
            name="uq_conversations_user_chat_revision",
        ),
    )
    op.create_index(
        "uq_conversations_active_chat",
        "conversations",
        ["telegram_chat_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_conversations_user_activity",
        "conversations",
        ["telegram_user_id", "last_activity_at"],
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE", name="fk_messages_conversation_id_conversations"),
            nullable=False,
        ),
        sa.Column("telegram_update_id", sa.BigInteger(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_retained", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("meta", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("role IN ('user', 'assistant', 'system', 'tool')", name="ck_messages_role_values"),
    )
    op.create_index(
        "uq_messages_telegram_update_id",
        "messages",
        ["telegram_update_id"],
        unique=True,
        postgresql_where=sa.text("telegram_update_id IS NOT NULL"),
    )
    op.create_index(
        "ix_messages_conversation_created", "messages", ["conversation_id", "created_at"]
    )

    op.create_table(
        "inbox_updates",
        sa.Column("update_id", sa.BigInteger(), primary_key=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'received'")),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "conversations.id",
                name="fk_inbox_updates_conversation_id_conversations",
            ),
            nullable=True,
        ),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("payload_digest", sa.CHAR(64), nullable=True),
        sa.CheckConstraint(
            "kind IN ('message', 'edited_message', 'callback_query', 'unsupported', 'invalid')",
            name="ck_inbox_updates_kind_values",
        ),
        sa.CheckConstraint(
            "status IN ('received', 'processed', 'ignored', 'failed', 'noticed')",
            name="ck_inbox_updates_status_values",
        ),
    )
    op.create_index("ix_inbox_updates_status", "inbox_updates", ["status"])


def downgrade() -> None:
    op.drop_index("ix_inbox_updates_status", table_name="inbox_updates")
    op.drop_table("inbox_updates")
    op.drop_index("ix_messages_conversation_created", table_name="messages")
    op.drop_index("uq_messages_telegram_update_id", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_conversations_user_activity", table_name="conversations")
    op.drop_index("uq_conversations_active_chat", table_name="conversations")
    op.drop_table("conversations")
