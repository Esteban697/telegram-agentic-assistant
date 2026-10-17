"""Tasks and the reminder dispatch ledger.

Revision ID: 0005_tasks
Revises: 0004_documents
Create Date: 2026-10-17

The dispatcher's guarantees live in these constraints (PLAN.md §5.2, §3.3):

* `uq_reminder_dispatches_occurrence` is what makes a *send* exactly-once per occurrence;
* `ix_tasks_due` is the partial index the claim scans;
* `next_notification_at` is **nullable** and is maintained by the dispatcher: `NULL` means "no
  further occurrence", which is how a delivered one-shot reminder leaves the claimable set
  (B1). The earlier design left a delivered reminder permanently due, so it was re-claimed
  every minute and eventually reported as `failed`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_tasks"
down_revision: str | None = "0004_documents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("next_notification_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("5")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("length(title) BETWEEN 1 AND 200", name="ck_tasks_title_length"),
        sa.CheckConstraint(
            "state IN ('pending', 'completed', 'cancelled', 'failed')", name="ck_tasks_state_values"
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_tasks_attempts_non_negative"),
    )
    op.create_index(
        "ix_tasks_due", "tasks", ["next_notification_at"], postgresql_where=sa.text("state = 'pending'")
    )
    op.create_index(
        "uq_tasks_user_idempotency_key", "tasks", ["user_id", "idempotency_key"], unique=True
    )

    op.create_table(
        "reminder_dispatches",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="CASCADE", name="fk_reminder_dispatches_task_id_tasks"),
            nullable=False,
        ),
        sa.Column("occurrence_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatch_key", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'claimed'")),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "state IN ('claimed', 'sent', 'failed', 'unknown')",
            name="ck_reminder_dispatches_state_values",
        ),
        sa.UniqueConstraint(
            "task_id", "occurrence_at", name="uq_reminder_dispatches_occurrence"
        ),
        sa.UniqueConstraint("dispatch_key", name="uq_reminder_dispatches_key"),
    )
    op.create_index(
        "ix_reminder_dispatches_open",
        "reminder_dispatches",
        ["state"],
        postgresql_where=sa.text("state IN ('claimed', 'unknown')"),
    )


def downgrade() -> None:
    op.drop_index("ix_reminder_dispatches_open", table_name="reminder_dispatches")
    op.drop_table("reminder_dispatches")
    op.drop_index("uq_tasks_user_idempotency_key", table_name="tasks")
    op.drop_index("ix_tasks_due", table_name="tasks")
    op.drop_table("tasks")
