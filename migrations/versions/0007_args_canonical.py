"""Add `pending_actions.args_canonical`, the float-safe form of the approved arguments.

Revision ID: 0007_args_canonical
Revises: 0006_actions
Create Date: 2026-10-17

`jsonb` does not round-trip floats faithfully (`1e20` returns as an `int`, `-0.0` as `0.0`), so
re-hashing `args_json` to check that an approval still matches what was stored produced a false
mismatch for legitimate float arguments and refused the action. The canonical serialisation is
therefore stored as text and hashed; `args_json` remains for querying.

The Phase 3 review found this before Phase 4 introduced prices, coordinates and scores.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_args_canonical"
down_revision: str | None = "0006_actions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pending_actions",
        sa.Column("args_canonical", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    # Backfill from the jsonb form: it is the closest available text, and the claim compares the
    # stored text with a re-canonicalisation *of itself*, so a non-canonical backfill is still
    # consistent with its own hash.
    op.execute("UPDATE pending_actions SET args_canonical = args_json::text WHERE args_canonical = ''")


def downgrade() -> None:
    op.drop_column("pending_actions", "args_canonical")
