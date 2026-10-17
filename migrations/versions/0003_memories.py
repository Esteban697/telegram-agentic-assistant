"""Explicit memory.

Revision ID: 0003_memories
Revises: 0002_conversations
Create Date: 2026-10-17

The partial unique index on `(user_id, key)` is what gives `save_preference` upsert semantics
(PLAN.md §3.2, §3.3): re-saving a preference cannot create a second live row.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_memories"
down_revision: str | None = "0002_conversations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memories",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("memory_type", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Numeric(3, 2), nullable=False, server_default=sa.text("1.00")),
        sa.Column("is_explicit", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(key) BETWEEN 1 AND 128", name="ck_memories_key_length"),
        sa.CheckConstraint("length(value) <= 4000", name="ck_memories_value_length"),
        sa.CheckConstraint("memory_type IN ('preference', 'fact')", name="ck_memories_type_values"),
        sa.CheckConstraint(
            "source IN ('explicit_user', 'confirmed')", name="ck_memories_source_values"
        ),
    )
    op.create_index(
        "uq_memories_user_key",
        "memories",
        ["user_id", "key"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_memories_user", "memories", ["user_id"], postgresql_where=sa.text("deleted_at IS NULL")
    )


def downgrade() -> None:
    op.drop_index("ix_memories_user", table_name="memories")
    op.drop_index("uq_memories_user_key", table_name="memories")
    op.drop_table("memories")
