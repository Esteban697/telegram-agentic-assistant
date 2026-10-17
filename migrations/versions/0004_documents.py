"""Documents, chunks with the HNSW vector index, and the active embedding model.

Revision ID: 0004_documents
Revises: 0003_memories
Create Date: 2026-10-17

Phase 4 implements the behaviour; the schema lands here because acceptance (a) requires every
table, the `vector` extension and the HNSW index to exist after `alembic upgrade head` on an
empty database, and because Phase 4 must not have to change the shape Phase 3 depends on.

Three load-bearing details:

* **HNSW, not IVFFlat** (decision 6): it can be built on an empty table, so it belongs in the
  migration rather than in a post-load step. 768 dimensions is far below pgvector's
  2,000-dimension *index* limit, so no `halfvec` workaround is needed.
* **The chunk key is three-part** (`document_id, pipeline_version, chunk_index`, RB3-follow-on):
  a retry inside one version converges, while the previous version's rows survive under the same
  `document_id` so the document stays answerable throughout its own re-index.
* **`embedding_models` is seeded with exactly one active row** (B7, acceptance (g)): retrieval
  and `/readyz` read the row, so it must exist before the first ingest. A migration cannot read
  application settings, hence the literal.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0004_documents"
down_revision: str | None = "0003_memories"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_MODEL = ("embeddinggemma", 768, 1)


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("sanitized_filename", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("gcs_uri", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.CHAR(64), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("ingest_status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("ingest_error", sa.Text(), nullable=True),
        sa.Column("pipeline_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delete_state", sa.Text(), nullable=False, server_default=sa.text("'none'")),
        sa.Column("delete_error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("size_bytes > 0", name="ck_documents_size_positive"),
        sa.CheckConstraint(
            "ingest_status IN ('pending', 'processing', 'ready', 'failed')",
            name="ck_documents_ingest_status_values",
        ),
        sa.CheckConstraint(
            "delete_state IN ('none', 'requested', 'chunks_deleted', 'object_deleted', 'complete', 'failed')",
            name="ck_documents_delete_state_values",
        ),
    )
    op.create_index(
        "uq_documents_user_hash_version",
        "documents",
        ["user_id", "content_sha256", "pipeline_version"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index("ix_documents_user_status", "documents", ["user_id", "ingest_status"])

    op.create_table(
        "document_chunks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE", name="fk_document_chunks_document_id_documents"),
            nullable=False,
        ),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("embedding", Vector(768), nullable=False),
        sa.Column("embed_model", sa.Text(), nullable=False),
        sa.Column("pipeline_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("chunk_index >= 0", name="ck_document_chunks_chunk_index_non_negative"),
        sa.CheckConstraint(
            "page_number IS NULL OR page_number >= 1", name="ck_document_chunks_page_number_positive"
        ),
        sa.UniqueConstraint(
            "document_id",
            "pipeline_version",
            "chunk_index",
            name="uq_document_chunks_version_chunk",
        ),
    )
    op.create_index("ix_document_chunks_owner", "document_chunks", ["owner_user_id"])
    op.create_index("ix_document_chunks_document", "document_chunks", ["document_id"])
    op.create_index("ix_document_chunks_embed_model", "document_chunks", ["embed_model"])
    op.create_index(
        "ix_document_chunks_embedding_hnsw",
        "document_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "embedding_models",
        sa.Column("model_name", sa.Text(), primary_key=True),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("pipeline_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("dim > 0", name="ck_embedding_models_dim_positive"),
    )
    op.create_index(
        "uq_embedding_models_active",
        "embedding_models",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    # Exactly one active row, so retrieval and /readyz always have an authority to read (B7).
    model_name, dim, version = SEED_MODEL
    op.execute(
        sa.text(
            "INSERT INTO embedding_models (model_name, dim, pipeline_version, is_active) "
            "VALUES (:name, :dim, :version, true) ON CONFLICT (model_name) DO NOTHING"
        ).bindparams(name=model_name, dim=dim, version=version)
    )


def downgrade() -> None:
    op.drop_index("uq_embedding_models_active", table_name="embedding_models")
    op.drop_table("embedding_models")
    op.drop_index("ix_document_chunks_embedding_hnsw", table_name="document_chunks")
    op.drop_index("ix_document_chunks_embed_model", table_name="document_chunks")
    op.drop_index("ix_document_chunks_document", table_name="document_chunks")
    op.drop_index("ix_document_chunks_owner", table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_index("ix_documents_user_status", table_name="documents")
    op.drop_index("uq_documents_user_hash_version", table_name="documents")
    op.drop_table("documents")
