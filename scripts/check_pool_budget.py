"""Connection-budget gate for the deploy — PLAN.md §11.2, §9.2, Appendix A.11.

    python scripts/check_pool_budget.py --database-sync-url-file ~/.taa/secrets/database-url
    python scripts/check_pool_budget.py --max-connections 50      # arithmetic only, no connection

**Why this is a script and not a comment.** §11.2 derives the Cloud SQL tier from an inequality:

    (DB_POOL_SIZE + CHECKPOINTER_POOL_SIZE) x concurrent_processes  <=  max_connections - 5

with 7 x 6 = 42 connections required and a 5-connection reserve, so the smallest tier that fits is
the one whose default `max_connections` is 50. Oversubscribing a connection pool is not a slowdown,
it is an outage: the last processes to start get "too many clients already" and their work fails.
So the deploy **measures** `SHOW max_connections` on the real instance and refuses if the
inequality does not hold, printing the two remedies rather than a stack trace.

`--database-sync-url-file` exists so the DSN — which contains the database password — is read from
a file instead of an argument, keeping it out of the shell history and out of `ps`. The same rule
as `scripts/deploy.sh`.

Exit codes: 0 = fits, 1 = does not fit, 2 = could not measure.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.storage.postgres import build_sync_engine  # noqa: E402


def required_connections(
    *, db_pool: int, checkpointer_pool: int, concurrent_processes: int
) -> int:
    """The arithmetic of §11.2, as a function so it can be asserted rather than restated."""

    return (db_pool + checkpointer_pool) * concurrent_processes


def verdict(*, required: int, reserve: int, max_connections: int) -> tuple[bool, str]:
    """The inequality, and a message that names the arithmetic and both remedies."""

    budget = required + reserve
    if budget <= max_connections:
        return True, (
            f"fits: {required} conexiones requeridas + {reserve} de reserva = {budget} "
            f"<= max_connections {max_connections}"
        )
    return False, (
        f"NO cabe: {required} requeridas + {reserve} de reserva = {budget} > "
        f"max_connections {max_connections}.\n"
        "  Dos salidas:\n"
        "   1. subir el tier de Cloud SQL (más memoria, más max_connections); o\n"
        "   2. bajar los pools: DB_POOL_SIZE y CHECKPOINTER_POOL_SIZE, con el sacrificio de\n"
        "      rendimiento que eso supone (con max_connections=25 hacen falta 2 y 2).\n"
        "  Sobresuscribir el pool no es una lentitud: los últimos procesos en arrancar reciben\n"
        "  'too many clients already' y su trabajo falla."
    )


def measure_max_connections(database_sync_url: str) -> int:
    """`SHOW max_connections` on the real instance (there is no offline equivalent)."""

    import sqlalchemy as sa

    engine = build_sync_engine(Settings(database_sync_url=database_sync_url))
    try:
        with engine.connect() as connection:
            return int(connection.execute(sa.text("SHOW max_connections")).scalar_one())
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Puerta del aritmético de conexiones (§11.2).")
    parser.add_argument("--database-sync-url-file", default="", help="fichero con la DSN")
    parser.add_argument("--database-sync-url", default="", help="DSN (evítalo: queda en el historial)")
    parser.add_argument("--max-connections", type=int, default=0, help="valor medido, si ya lo tienes")
    parser.add_argument("--concurrent-processes", type=int, default=6)
    parser.add_argument("--reserve", type=int, default=5)
    parser.add_argument("--db-pool-size", type=int, default=0)
    parser.add_argument("--checkpointer-pool-size", type=int, default=0)
    parser.add_argument(
        "--use-app-defaults",
        action="store_true",
        help=(
            "usa los valores por defecto de la clase Settings, no los del .env de esta máquina: "
            "el despliegue no fija DB_POOL_SIZE ni CHECKPOINTER_POOL_SIZE en el entorno del "
            "contenedor, así que el contenedor usa los defectos — y el aritmético debe describir "
            "el despliegue, no la máquina del operador"
        ),
    )
    args = parser.parse_args(argv)

    if args.use_app_defaults:
        # `Settings()` reads `.env`; the deployment's containers do not get these variables at all.
        defaults = Settings.model_fields
        db_pool = defaults["db_pool_size"].default
        checkpointer_pool = defaults["checkpointer_pool_size"].default
    else:
        settings = Settings()
        db_pool = settings.db_pool_size
        checkpointer_pool = settings.checkpointer_pool_size
    db_pool = args.db_pool_size or db_pool
    checkpointer_pool = args.checkpointer_pool_size or checkpointer_pool
    required = required_connections(
        db_pool=db_pool,
        checkpointer_pool=checkpointer_pool,
        concurrent_processes=args.concurrent_processes,
    )

    if args.max_connections:
        max_connections = args.max_connections
        source = "valor pasado por argumento"
    else:
        dsn = args.database_sync_url
        if args.database_sync_url_file:
            path = pathlib.Path(args.database_sync_url_file)
            if not path.is_file():
                print(f"error: no existe {path}", file=sys.stderr)
                return 2
            dsn = path.read_text(encoding="utf-8").strip()
        if not dsn:
            print(
                "error: hace falta --database-sync-url-file (o --max-connections) para medir",
                file=sys.stderr,
            )
            return 2
        try:
            max_connections = measure_max_connections(dsn)
        except Exception as exc:  # noqa: BLE001 - a gate that cannot measure must not pass
            print(f"error: no se pudo medir max_connections: {type(exc).__name__}", file=sys.stderr)
            return 2
        source = "SHOW max_connections en la instancia"

    ok, message = verdict(required=required, reserve=args.reserve, max_connections=max_connections)
    print(f"pools: DB_POOL_SIZE={db_pool} CHECKPOINTER_POOL_SIZE={checkpointer_pool}")
    print(f"procesos concurrentes: {args.concurrent_processes} ({source}: {max_connections})")
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
