"""Create the schema from an empty database, then the checkpointer's own tables.

    python scripts/bootstrap_db.py

Order (PLAN.md §3.4):

1. `alembic upgrade head` — our tables, the `vector` extension and the HNSW index;
2. `PostgresSaver.setup()` — the LangGraph checkpoint tables, which the library owns
   (decision 8: vendoring their DDL into a migration would couple us to its internals);
3. verification — every expected table exists, `vector` is ≥ 0.8, the HNSW index exists, and
   exactly one `embedding_models` row is active (acceptance (a) and (g)).

Re-running it is safe: every step is idempotent.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings  # noqa: E402
from app.storage.checkpointing import CHECKPOINT_TABLES, build_checkpointer, setup_checkpointer  # noqa: E402
from app.storage.models import ALL_TABLES  # noqa: E402
from app.storage.postgres import build_sync_engine, use_compatible_event_loop  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def alembic_config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def upgrade_schema() -> None:
    print("· alembic upgrade head")
    command.upgrade(alembic_config(), "head")


async def setup_checkpoint_tables() -> None:
    print("· PostgresSaver.setup()")
    settings = load_settings().validate_or_raise()
    checkpointer = await build_checkpointer(settings)
    try:
        await setup_checkpointer(checkpointer)
    finally:
        await checkpointer.aclose()


def verify() -> int:
    """Assert the schema is what the plan says it must be. Returns a process exit code."""

    settings = load_settings().validate_or_raise()
    engine = build_sync_engine(settings)
    problems: list[str] = []
    with engine.connect() as connection:
        expected = {model.__tablename__ for model in ALL_TABLES}
        present = set(
            connection.execute(
                sa.text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
            ).scalars()
        )

        missing = sorted(expected - present)
        if missing:
            problems.append(f"missing tables: {missing}")
        missing_ck = sorted(set(CHECKPOINT_TABLES) - present)
        if missing_ck:
            problems.append(f"missing checkpoint tables: {missing_ck}")

        version = connection.execute(
            sa.text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        ).scalar_one_or_none()
        if version is None:
            problems.append("the 'vector' extension is not installed")
        else:
            parts = tuple(int(p) for p in str(version).split(".")[:2])
            if parts < (0, 8):
                problems.append(f"pgvector {version} < 0.8 (hnsw.iterative_scan needs 0.8)")

        hnsw = connection.execute(
            sa.text(
                "SELECT indexdef FROM pg_indexes WHERE tablename = 'document_chunks' "
                "AND indexdef ILIKE '%USING hnsw%'"
            )
        ).scalar_one_or_none()
        if hnsw is None:
            problems.append("the HNSW index on document_chunks.embedding is missing")

        trigger = connection.execute(
            sa.text(
                "SELECT tgname FROM pg_trigger WHERE tgname = 'pending_actions_args_immutable'"
            )
        ).scalar_one_or_none()
        if trigger is None:
            problems.append("the pending_actions_args_immutable trigger is missing")

        active_models = connection.execute(
            sa.text("SELECT count(*) FROM embedding_models WHERE is_active")
        ).scalar_one()
        if active_models != 1:
            problems.append(f"expected exactly 1 active embedding_models row, found {active_models}")

    engine.dispose()

    if problems:
        print("\nBOOTSTRAP FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print(
        "\nOK: "
        f"{len(ALL_TABLES)} application tables + {len(CHECKPOINT_TABLES)} checkpoint tables, "
        f"pgvector {version}, HNSW index present, trigger present, 1 active embedding model."
    )
    return 0


def main() -> int:
    # Before any loop exists: psycopg's async mode cannot use Windows' default ProactorEventLoop.
    use_compatible_event_loop()
    upgrade_schema()
    asyncio.run(setup_checkpoint_tables())
    return verify()


if __name__ == "__main__":
    sys.exit(main())
