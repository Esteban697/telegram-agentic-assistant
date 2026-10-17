"""Approvals, the tool ledger and the resume queue — plus the arguments-immutability trigger.

Revision ID: 0006_actions
Revises: 0005_tasks
Create Date: 2026-10-17

The behaviour of these tables arrives in Phase 3; their shape is fixed here because §3.4
specifies one ordered revision list, and because acceptance (g) requires the
`pending_actions_args_immutable` trigger to exist after `alembic upgrade head` and a direct
`UPDATE` of an approved row's `args_json` to **raise**.

The trigger enforces exactly what SQL can express (RN10): a stored `args_json`/`args_revision`/
`args_hash` cannot change, with one permitted exception — an explicit re-presentation
(`args_revision + 1` while the row is `pending`). It deliberately does **not** try to verify
that the new hash matches the new arguments: that would need Python-side canonical JSON inside
SQL. That check belongs to the application transaction.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_actions"
down_revision: str | None = "0005_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRIGGER_NAME = "pending_actions_args_immutable"
FUNCTION_NAME = "pending_actions_args_immutable_fn"

# psycopg validates placeholders even through `exec_driver_sql`, so the `%` markers in the
# RAISE format string are written as `%%`: psycopg turns each into a literal `%`, which is what
# plpgsql then sees. (Written the other way round, the migration fails with
# "only '%s', '%b', '%t' are allowed as placeholders".)
CREATE_FUNCTION_SQL = f"""
CREATE OR REPLACE FUNCTION {FUNCTION_NAME}() RETURNS trigger AS $$
BEGIN
    IF NEW.args_json IS NOT DISTINCT FROM OLD.args_json
       AND NEW.args_revision IS NOT DISTINCT FROM OLD.args_revision
       AND NEW.args_hash IS NOT DISTINCT FROM OLD.args_hash THEN
        RETURN NEW;
    END IF;

    -- The single permitted exception: an explicit re-presentation of the same action.
    IF NEW.args_revision = OLD.args_revision + 1 AND NEW.state = 'pending' THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION
        'pending_actions arguments are immutable (action_id=%%, revision %%, state %%)',
        OLD.action_id, OLD.args_revision, OLD.state
        USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;
"""

CREATE_TRIGGER_SQL = f"""
CREATE TRIGGER {TRIGGER_NAME}
    BEFORE UPDATE ON pending_actions
    FOR EACH ROW
    EXECUTE FUNCTION {FUNCTION_NAME}();
"""


def upgrade() -> None:
    op.create_table(
        "pending_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("action_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("args_json", postgresql.JSONB(), nullable=False),
        sa.Column("args_hash", sa.CHAR(64), nullable=False),
        sa.Column("args_revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("preview_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.BigInteger(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_json", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "retry_of",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pending_actions.id", name="fk_pending_actions_retry_of_pending_actions"),
            nullable=True,
        ),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending', 'approved', 'rejected', 'expired', 'executing', 'succeeded', "
            "'failed', 'unknown')",
            name="ck_pending_actions_state_values",
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_pending_actions_expires_after_creation"),
        sa.UniqueConstraint("action_id", name="uq_pending_actions_action_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_pending_actions_idempotency_key"),
    )
    op.create_index(
        "uq_pending_actions_live",
        "pending_actions",
        ["user_id", "action_type", "args_hash"],
        unique=True,
        postgresql_where=sa.text("state IN ('pending', 'approved', 'executing')"),
    )
    op.create_index(
        "ix_pending_actions_live_expiry",
        "pending_actions",
        ["state", "expires_at"],
        postgresql_where=sa.text("state IN ('pending', 'approved')"),
    )
    op.create_index(
        "ix_pending_actions_executing",
        "pending_actions",
        ["state", "claimed_at"],
        postgresql_where=sa.text("state IN ('executing', 'unknown')"),
    )

    bind = op.get_bind()
    bind.exec_driver_sql(CREATE_FUNCTION_SQL)
    bind.exec_driver_sql(CREATE_TRIGGER_SQL)

    op.create_table(
        "tool_invocations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("run_key", sa.Text(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("tool_call_id", sa.Text(), nullable=True),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("args_hash", sa.CHAR(64), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'started'")),
        sa.Column("result_json", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('started', 'succeeded', 'failed', 'unknown')",
            name="ck_tool_invocations_state_values",
        ),
        sa.UniqueConstraint("thread_id", "run_key", "attempt_no", name="uq_tool_invocations_run"),
    )

    op.create_table(
        "work_items",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", name="fk_work_items_conversation_id_conversations"),
            nullable=False,
        ),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("5")),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('resume_thread', 'deliver_reply')", name="ck_work_items_kind_values"),
        sa.CheckConstraint(
            "state IN ('pending', 'claimed', 'done', 'failed', 'dead')", name="ck_work_items_state_values"
        ),
    )
    op.create_index(
        "ix_work_items_due", "work_items", ["run_at"], postgresql_where=sa.text("state = 'pending'")
    )
    op.create_index(
        "uq_work_items_open",
        "work_items",
        ["thread_id", "kind"],
        unique=True,
        postgresql_where=sa.text("state IN ('pending', 'claimed')"),
    )


def downgrade() -> None:
    op.drop_index("uq_work_items_open", table_name="work_items")
    op.drop_index("ix_work_items_due", table_name="work_items")
    op.drop_table("work_items")
    op.drop_table("tool_invocations")
    bind = op.get_bind()
    bind.exec_driver_sql(f"DROP TRIGGER IF EXISTS {TRIGGER_NAME} ON pending_actions")
    bind.exec_driver_sql(f"DROP FUNCTION IF EXISTS {FUNCTION_NAME}()")
    op.drop_index("ix_pending_actions_executing", table_name="pending_actions")
    op.drop_index("ix_pending_actions_live_expiry", table_name="pending_actions")
    op.drop_index("uq_pending_actions_live", table_name="pending_actions")
    op.drop_table("pending_actions")
