"""Alembic environment (PLAN.md §3.4).

Three decisions live here:

1. **The URL comes from configuration**, not from ``alembic.ini``: migrations run against the
   DSN the deployment provides (``DATABASE_SYNC_URL``), so nothing is committed and local,
   CI and (later) Cloud SQL all use the same path.
2. **The checkpointer's tables are excluded** from autogenerate. ``PostgresSaver.setup()`` owns
   them; vendoring their DDL into a revision would hard-couple us to library internals and
   break on upgrade (decision 8, spec §8 "no reinventar su formato").
3. **``compare_type`` is on and ``Vector`` has a renderer**, so a pgvector column does not make
   the diff oscillate.

Migrations run on a **sync** engine: they are not part of the request path, and sync Alembic is
the well-trodden path (decision 1).
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

import sqlalchemy as sa
from alembic import context

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings  # noqa: E402
from app.storage.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Tables owned by langgraph-checkpoint-postgres (`PostgresSaver.setup()`), discovered from the
# installed package rather than assumed (Appendix B.2).
CHECKPOINTER_TABLES = frozenset(
    {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"}
)


def _sync_url() -> str:
    """Resolve the URL to migrate with, preferring DATABASE_SYNC_URL."""

    override = context.get_x_argument(as_dictionary=True).get("url")
    if override:
        return override
    settings = load_settings()
    url = settings.database_sync_url.get_secret_value()
    if not url:
        raise SystemExit(
            "DATABASE_SYNC_URL is not set. Migrations need the synchronous DSN "
            "(the application's DATABASE_URL is async-only)."
        )
    # ConfigParser interpolates `%`, so a password containing one must be escaped.
    return url.replace("%", "%%")


def include_object(obj, name, type_, reflected, compare_to) -> bool:  # noqa: ANN001
    """Keep the checkpointer's objects out of the migration graph entirely."""

    if type_ == "table" and name in CHECKPOINTER_TABLES:
        return False
    if type_ in {"index", "unique_constraint", "foreign_key_constraint"}:
        table = getattr(obj, "table", None)
        if table is not None and table.name in CHECKPOINTER_TABLES:
            return False
    return True


def render_item(type_, obj, autogen_context) -> str | bool:  # noqa: ANN001
    """Render pgvector columns with an explicit import instead of a repr."""

    if type_ == "type" and obj.__class__.__module__.startswith("pgvector"):
        autogen_context.imports.add("from pgvector.sqlalchemy import Vector")
        return f"Vector({getattr(obj, 'dim', None)})"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # NullPool: a migration is a short-lived script; pooling would keep a connection alive
    # after the process is done with it.
    connectable = sa.create_engine(_sync_url(), poolclass=sa.pool.NullPool, future=True)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
