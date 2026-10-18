"""Connection-budget gate for the deploy — PLAN.md §11.2, §9.2; Supabase decision (2026-10-18).

    python scripts/check_pool_budget.py --use-app-defaults                     # los pools desplegados
    python scripts/check_pool_budget.py --db-pool-size 2 --checkpointer-pool-size 1
    TAA_POOLER_MAX_CONNECTIONS=14 python scripts/check_pool_budget.py

**The binding limit is the pooler's, not the server's.** The database is Supabase behind
Supavisor in **session mode on port 5432**, and that mode allows **14 client connections** —
measured: 1–14 succeed, the 15th fails immediately with `EMAXCONNSESSION`, which is a hard failure
and not a wait. The server-side `max_connections` is 60 and is irrelevant here; sizing against it
would oversubscribe the pooler by four times.

The inequality this script enforces:

    (DB_POOL_SIZE + CHECKPOINTER_POOL_SIZE) x concurrent_processes + reserve <= pooler_limit

With `--max-instances=1` (mandatory now, not a cost choice) the concurrent processes are the
service plus the three jobs when they overlap, i.e. **4**. The deployed values are
`DB_POOL_SIZE=2`, `CHECKPOINTER_POOL_SIZE=1`, reserve 2: (2 + 1) x 4 + 2 = **14 <= 14** — it fits
exactly, with the two spare connections the reserve is for.

`--use-app-defaults` is what the deploy uses: it reads the **class defaults**, not this machine's
`.env`, because the containers are given the pool sizes explicitly and the arithmetic has to
describe the deployment rather than the operator's laptop.

Exit codes: 0 = fits, 1 = does not fit (and the deploy refuses), 2 = bad input.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402

# The Supavisor session-mode ceiling, measured. It is a declared input on purpose: it is a property
# of the provider's plan, not of the server, and it must be visible where the arithmetic is made.
DEFAULT_POOLER_MAX_CONNECTIONS = 14
# The service plus the three jobs, which can overlap: with `--max-instances=1` this is the real
# worst case.
DEFAULT_CONCURRENT_PROCESSES = 4
DEFAULT_RESERVE = 2


def required_connections(
    *, db_pool: int, checkpointer_pool: int, concurrent_processes: int
) -> int:
    """The first half of the inequality, as a function so it can be asserted not restated."""

    return (db_pool + checkpointer_pool) * concurrent_processes


def verdict(
    *,
    required: int,
    reserve: int,
    pooler_limit: int,
    concurrent_processes: int = DEFAULT_CONCURRENT_PROCESSES,
) -> tuple[bool, str]:
    """The inequality, and a message that names the arithmetic and the remedies."""

    budget = required + reserve
    if budget <= pooler_limit:
        return True, (
            f"cabe: {required} conexiones requeridas + {reserve} de reserva = {budget} "
            f"<= límite del pooler {pooler_limit}"
        )
    per_process = (pooler_limit - reserve) // max(1, concurrent_processes)
    return False, (
        f"NO cabe: {required} requeridas + {reserve} de reserva = {budget} > "
        f"límite del pooler {pooler_limit} (Supavisor, modo sesión).\n"
        f"  El pooler no espera: la conexión número {pooler_limit + 1} falla al instante con "
        "EMAXCONNSESSION.\n"
        "  Salidas:\n"
        "    1. bajar los pools: con "
        f"{concurrent_processes} procesos y {reserve} de reserva, DB_POOL_SIZE + "
        f"CHECKPOINTER_POOL_SIZE no puede pasar de {per_process}; o\n"
        "    2. bajar los procesos concurrentes — pero `--max-instances=1` es obligatorio y los "
        "tres\n       jobs pueden solaparse con el servicio, así que la palanca práctica es la 1.\n"
        "  Sobresuscribir el pooler no es una lentitud: es un fallo inmediato en el arranque."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Puerta del aritmético de conexiones (Supabase).")
    parser.add_argument(
        "--pooler-max-connections",
        type=int,
        default=0,
        help=(
            "límite del pooler en modo sesión; por defecto TAA_POOLER_MAX_CONNECTIONS "
            f"({DEFAULT_POOLER_MAX_CONNECTIONS})"
        ),
    )
    parser.add_argument(
        "--concurrent-processes", type=int, default=DEFAULT_CONCURRENT_PROCESSES
    )
    parser.add_argument("--reserve", type=int, default=DEFAULT_RESERVE)
    parser.add_argument("--db-pool-size", type=int, default=0)
    parser.add_argument("--checkpointer-pool-size", type=int, default=0)
    parser.add_argument(
        "--use-app-defaults",
        action="store_true",
        help=(
            "usa los valores por defecto de la clase Settings, no los del .env de esta máquina: "
            "los contenedores reciben DB_POOL_SIZE y CHECKPOINTER_POOL_SIZE explícitos, y el "
            "aritmético debe describir el despliegue, no la máquina del operador"
        ),
    )
    args = parser.parse_args(argv)

    import os

    if args.use_app_defaults:
        fields = Settings.model_fields
        db_pool = fields["db_pool_size"].default
        checkpointer_pool = fields["checkpointer_pool_size"].default
    else:
        settings = Settings()
        db_pool = settings.db_pool_size
        checkpointer_pool = settings.checkpointer_pool_size
    db_pool = args.db_pool_size or db_pool
    checkpointer_pool = args.checkpointer_pool_size or checkpointer_pool

    pooler_limit = args.pooler_max_connections or int(
        os.environ.get("TAA_POOLER_MAX_CONNECTIONS", DEFAULT_POOLER_MAX_CONNECTIONS)
    )
    if pooler_limit <= 0 or args.concurrent_processes <= 0 or args.reserve < 0:
        print("error: límites y procesos deben ser positivos", file=sys.stderr)
        return 2

    required = required_connections(
        db_pool=db_pool,
        checkpointer_pool=checkpointer_pool,
        concurrent_processes=args.concurrent_processes,
    )
    ok, message = verdict(
        required=required,
        reserve=args.reserve,
        pooler_limit=pooler_limit,
        concurrent_processes=args.concurrent_processes,
    )
    print(f"pools: DB_POOL_SIZE={db_pool} CHECKPOINTER_POOL_SIZE={checkpointer_pool}")
    print(
        f"procesos concurrentes: {args.concurrent_processes} "
        f"(1 servicio + 3 jobs solapables, --max-instances=1)"
    )
    print(f"límite del pooler (Supavisor, modo sesión): {pooler_limit}")
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
