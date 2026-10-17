"""Extensions: `vector` (and the version guard that `hnsw.iterative_scan` needs).

Revision ID: 0001_extensions
Revises:
Create Date: 2026-10-17

PLAN.md §3.4 step 2: the first revision creates the extension and refuses to continue if the
installed version is older than 0.8, because `SET LOCAL hnsw.iterative_scan = strict_order`
(a 0.8.0 feature) is what makes a *filtered* vector search return exactly `top_k` rows. A
database that silently lacked it would return fewer rows and the RAG path would answer "no
data" instead of failing loudly.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_extensions"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MINIMUM_VECTOR_VERSION = (0, 8)


def upgrade() -> None:
    # `vector` is the only extension this schema needs. `gen_random_uuid()` has been core
    # since PostgreSQL 13, so `pgcrypto` is not required.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    installed = op.get_bind().execute(
        sa.text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    ).scalar_one_or_none()

    if installed is None:  # pragma: no cover - CREATE EXTENSION above would have failed
        raise RuntimeError("the 'vector' extension is not installed after CREATE EXTENSION")

    parts = tuple(int(part) for part in str(installed).split(".")[:2])
    if parts < MINIMUM_VECTOR_VERSION:
        raise RuntimeError(
            f"pgvector {installed} is older than the required "
            f"{'.'.join(str(p) for p in MINIMUM_VECTOR_VERSION)}: `hnsw.iterative_scan` is a "
            "0.8.0 feature and filtered vector search would silently return too few rows. "
            "Upgrade the extension (Cloud SQL provides 0.8.5 for PostgreSQL 13+)."
        )


def downgrade() -> None:
    # Deliberately not dropped: other schemas in the same database may use the extension, and
    # dropping it would destroy the chunk embeddings of a newer revision.
    pass
