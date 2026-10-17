"""Engines, session factories and the database handle (PLAN.md §10 Phase 2, decision 1).

Layer 1: imports only layer 0.

Two engines exist on purpose:

* **async** (`DATABASE_URL`) for the application — the whole request path is async, and the
  async driver is what keeps the event loop from blocking (decision 1);
* **sync** (`DATABASE_SYNC_URL`) for Alembic and the scripts — migrations are not part of the
  request path, and sync Alembic is the well-trodden path.

The pool sizes are part of the connection arithmetic of §11.2:
``(DB_POOL_SIZE + CHECKPOINTER_POOL_SIZE) x concurrent processes <= tier max_connections - 5``.
``max_overflow`` is therefore 0: the bound must be the real bound, not a number the pool is
allowed to exceed when it feels like it.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings

_logger = logging.getLogger(__name__)


def use_compatible_event_loop() -> bool:
    """Select an event loop psycopg's async mode can run on.

    **Windows only, and not optional.** psycopg 3's async mode refuses the default
    ``ProactorEventLoop``::

        InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async mode.

    Everything async that touches PostgreSQL — the ORM engine, `AsyncPostgresSaver` — would
    fail on Windows without this, so the policy is set before any loop is created (uvicorn and
    `asyncio.run` both honour the current policy). It is a no-op on Linux, which is why Cloud
    Run is unaffected; the alternative would be a second, synchronous code path just for the
    developer machine, which contradicts decision 1.

    Returns True when a policy was installed.
    """

    if sys.platform != "win32":  # pragma: no cover - Linux/Cloud Run
        return False
    policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy is None:  # pragma: no cover - defensive
        return False
    if not isinstance(asyncio.get_event_loop_policy(), policy):
        asyncio.set_event_loop_policy(policy())
        _logger.info(
            "selected the selector event loop for psycopg's async mode",
            extra={"platform": sys.platform},
        )
    return True


@dataclass(slots=True)
class Database:
    """The application's database handle: one async engine and its session factory."""

    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]

    async def dispose(self) -> None:
        await self.engine.dispose()

    def session(self) -> AsyncSession:
        return self.sessions()


def build_database(settings: Settings) -> Database:
    """Create the async engine from configuration.

    `pool_pre_ping` matters for a long-lived Cloud Run instance talking to a database that may
    have dropped an idle connection; without it the first request after a quiet period fails.
    """

    use_compatible_event_loop()
    url = settings.database_url.get_secret_value()
    if not url:
        raise ValueError("DATABASE_URL is not set")

    engine = create_async_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=0,
        pool_pre_ping=True,
        pool_recycle=1800,
        future=True,
    )
    return Database(engine=engine, sessions=async_sessionmaker(engine, expire_on_commit=False))


def build_sync_engine(settings: Settings) -> sa.Engine:
    """Create the synchronous engine the scripts and Alembic use."""

    url = settings.database_sync_url.get_secret_value() or settings.database_url.get_secret_value()
    if not url:
        raise ValueError("DATABASE_SYNC_URL is not set")
    return sa.create_engine(url, poolclass=sa.pool.NullPool, future=True)


def build_sync_session_factory(settings: Settings) -> sessionmaker[Session]:
    return sessionmaker(build_sync_engine(settings), expire_on_commit=False)


__all__ = [
    "Database",
    "build_database",
    "build_sync_engine",
    "build_sync_session_factory",
    "use_compatible_event_loop",
]
