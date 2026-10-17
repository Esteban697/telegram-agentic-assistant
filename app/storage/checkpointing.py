"""LangGraph checkpoint persistence (PLAN.md §10 Phase 2, decision 8).

Layer 2. Imports layer 0-1 and the checkpointer library.

**Why a second pool exists.** `langgraph-checkpoint-postgres` is constructed from a connection
string (or a psycopg pool) and brings its own `psycopg` + `psycopg-pool`; it cannot share the
ORM's SQLAlchemy pool. The savers *do* accept an injected connection, but a manually supplied
one must be `autocommit=True` with `row_factory=dict_row` (Appendix A.5) and interleaving the
saver's transaction semantics with the ORM's unit of work would put both at risk. So: two
pools, sized explicitly, and the arithmetic in §11.2 counts both. Two pools also means **two
ordered transactions, not one boundary** — which is why the handler enqueues its watchdog row
*before* the graph runs rather than relying on atomicity (RB1).

**Why `setup()` is not a migration.** Spec §8 says not to reinvent the checkpointer's format,
so its DDL belongs to the library: `scripts/bootstrap_db.py` runs `setup()` after
`alembic upgrade head`, and `migrations/env.py` excludes its tables from autogenerate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.storage.postgres import use_compatible_event_loop

_logger = logging.getLogger(__name__)

# The library's own tables, discovered from the installed package (Appendix B.2).
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations")


def to_psycopg_dsn(sqlalchemy_url: str) -> str:
    """Translate a SQLAlchemy DSN into the plain libpq DSN psycopg expects.

    ``postgresql+psycopg://user:pass@host/db`` is a SQLAlchemy spelling; psycopg would reject
    the ``+psycopg`` driver suffix.
    """

    for prefix in ("postgresql+psycopg://", "postgresql+asyncpg://"):
        if sqlalchemy_url.startswith(prefix):
            return "postgresql://" + sqlalchemy_url[len(prefix) :]
    return sqlalchemy_url


@dataclass
class Checkpointer:
    """An opened checkpointer, its pool, and the saver the graph is compiled with."""

    pool: AsyncConnectionPool
    saver: AsyncPostgresSaver

    async def aclose(self) -> None:
        await self.pool.close()


async def build_checkpointer(settings: Settings) -> Checkpointer:
    """Open the checkpointer's pool. Does **not** create the tables (that is `setup()`)."""

    use_compatible_event_loop()
    dsn = settings.database_sync_url.get_secret_value() or settings.database_url.get_secret_value()
    if not dsn:
        raise ValueError("a database DSN is required to build the checkpointer")

    pool = AsyncConnectionPool(
        conninfo=to_psycopg_dsn(dsn),
        min_size=1,
        max_size=settings.checkpointer_pool_size,
        open=False,
        # The saver requires both of these (Appendix A.5): autocommit so `setup()` commits its
        # DDL, and dict_row because it reads rows by name.
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    await pool.open()
    saver = AsyncPostgresSaver(pool)
    _logger.info(
        "checkpointer pool opened",
        extra={"pool_max_size": settings.checkpointer_pool_size, "status": "ready"},
    )
    return Checkpointer(pool=pool, saver=saver)


async def setup_checkpointer(checkpointer: Checkpointer) -> None:
    """Create the checkpoint tables if they are missing. Idempotent."""

    await checkpointer.saver.setup()


__all__ = [
    "CHECKPOINT_TABLES",
    "Checkpointer",
    "build_checkpointer",
    "setup_checkpointer",
    "to_psycopg_dsn",
]
