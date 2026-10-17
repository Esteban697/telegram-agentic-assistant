"""Alembic wrapper with a pre-flight check and a production guard (PLAN.md §3.4, §11.6).

    python scripts/migrate.py upgrade [revision]
    python scripts/migrate.py downgrade <revision>
    python scripts/migrate.py current
    python scripts/migrate.py history

The wrapper does three things the bare CLI does not:

* prints the current and target revision before touching anything, so a mistaken target is
  visible rather than destructive;
* refuses `downgrade` in production unless `--i-have-a-backup` is passed explicitly — the
  honest rollback path for a destructive revision is a Cloud SQL restore, not a downgrade;
* turns a connection failure into one clear sentence instead of a SQLAlchemy traceback.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings  # noqa: E402
from app.storage.postgres import build_sync_engine  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def alembic_config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def current_revision() -> str | None:
    settings = load_settings().validate_or_raise()
    try:
        engine = build_sync_engine(settings)
    except ValueError as exc:
        print(f"cannot migrate: {exc}")
        return None
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    except sa.exc.OperationalError as exc:
        print(f"cannot reach the database: {str(exc)[:160]}")
        return None
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["upgrade", "downgrade", "current", "history"])
    parser.add_argument("revision", nargs="?", default="head")
    parser.add_argument(
        "--i-have-a-backup",
        action="store_true",
        help="required to downgrade in production: downgrades are destructive",
    )
    args = parser.parse_args(argv)

    settings = load_settings().validate_or_raise()
    before = current_revision()
    print(f"current revision: {before or '(none)'}")

    if args.action == "current":
        return 0
    if args.action == "history":
        command.history(alembic_config(), verbose=False)
        return 0
    if args.action == "downgrade":
        if settings.is_production and not args.i_have_a_backup:
            print(
                "refusing to downgrade in production without --i-have-a-backup: a downgrade "
                "can drop columns and tables, and the documented recovery path is a Cloud SQL "
                "restore (PLAN.md §11.6)"
            )
            return 2
        print(f"target revision: {args.revision}")
        command.downgrade(alembic_config(), args.revision)
    else:
        print(f"target revision: {args.revision}")
        command.upgrade(alembic_config(), args.revision)

    print(f"revision after: {current_revision() or '(none)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
