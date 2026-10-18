"""Connectivity and schema checks for the Supabase database — PLAN.md §11.6.

    python scripts/check_database.py --database-url-file ~/.taa/secrets/database-url
    python scripts/check_database.py --database-url-file … --expected-extension-schema extensions

**Why these checks exist.** The database is Supabase (PostgreSQL 17.11 + pgvector 0.8.2 behind
Supavisor, session mode, port 5432). Two of its properties fail *late* if they are wrong:

* the **connectivity** — the pooler is reached over TLS from Cloud Run's IPv4 egress, and a wrong
  host or a `sslmode=disable` DSN only shows up when a request needs the database;
* the **schema path** — `vector` is installed in the `extensions` schema, not `public`. If the
  database `search_path` does not include it, or the app role lacks `USAGE` on it, the first
  `SELECT … ORDER BY embedding <=> …` fails at the first embedding/retrieval, not at startup. That
  is the failure this script converts into a deploy-time error.

The DSN is read from a **file** (`--database-url-file`) so the password never enters argv, the
shell history or `ps` — the same rule as `scripts/deploy.sh`.

Exit codes: 0 = everything checked out, 1 = a check failed, 2 = could not run the checks.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from dataclasses import dataclass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

MINIMUM_VECTOR_VERSION = (0, 8)
SUPABASE_EXTENSION_SCHEMA = "extensions"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str


def parse_version(value: str) -> tuple[int, ...]:
    """`"0.8.2"` → `(0, 8, 2)`. Non-numeric parts are ignored, missing ones are zero."""

    parts: list[int] = []
    for component in str(value).split(".")[:3]:
        digits = "".join(character for character in component if character.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


def version_at_least(value: str, minimum: tuple[int, ...]) -> bool:
    current = parse_version(value)
    width = max(len(current), len(minimum))
    return current + (0,) * (width - len(current)) >= minimum + (0,) * (width - len(minimum))


def _scalar(connection, statement: str):  # noqa: ANN001, ANN202 - a sqlalchemy Connection
    import sqlalchemy as sa

    return connection.execute(sa.text(statement)).scalar_one_or_none()


def checks(connection, *, expected_schema: str = SUPABASE_EXTENSION_SCHEMA) -> list[Check]:
    """Every check, against anything that answers `execute(sql).scalar_one_or_none()`.

    Written against that tiny surface so the failure modes can be exercised without a database.
    """

    results: list[Check] = []

    try:
        results.append(Check("conectividad", _scalar(connection, "SELECT 1") == 1, "SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - a connection error is the first finding
        results.append(Check("conectividad", False, f"{type(exc).__name__}: {exc}"))
        return results

    try:
        version = str(
            _scalar(connection, "SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            or ""
        )
    except Exception as exc:  # noqa: BLE001
        results.append(Check("pgvector instalado", False, f"{type(exc).__name__}: {exc}"))
        return results
    results.append(
        Check(
            "pgvector >= 0.8 (hnsw.iterative_scan)",
            version_at_least(version, MINIMUM_VECTOR_VERSION),
            f"extversion={version or '<ausente>'}",
        )
    )

    try:
        schema = _scalar(
            connection,
            "SELECT n.nspname FROM pg_extension e "
            "JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'vector'",
        )
    except Exception as exc:  # noqa: BLE001
        schema = None
        results.append(Check("esquema de vector", False, f"{type(exc).__name__}: {exc}"))
    else:
        results.append(
            Check(
                "esquema de vector",
                str(schema) == expected_schema,
                f"instalado en {schema!r}, esperado {expected_schema!r}",
            )
        )

    # Resolution, not just installation: this is the check that fails when the search_path is
    # wrong, and it fails here rather than at the first embedding.
    try:
        cast = _scalar(connection, "SELECT '[1,2,3]'::vector")
        results.append(Check("vector resuelve desde el rol de la aplicación", True, str(cast)))
    except Exception as exc:  # noqa: BLE001
        results.append(
            Check(
                "vector resuelve desde el rol de la aplicación",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        )

    try:
        iterative = _scalar(connection, "SELECT current_setting('hnsw.iterative_scan', true)")
        results.append(
            Check(
                "hnsw.iterative_scan disponible",
                iterative is not None,
                f"current_setting={iterative!r}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        results.append(Check("hnsw.iterative_scan disponible", False, f"{type(exc).__name__}: {exc}"))

    try:
        import sqlalchemy as sa

        role, superuser, createdb, createrole = connection.execute(
            sa.text(
                "SELECT current_user, rolsuper, rolcreatedb, rolcreaterole "
                "FROM pg_roles WHERE rolname = current_user"
            )
        ).one()
        results.append(
            Check(
                "rol de la aplicación sin privilegios de administración",
                not (superuser or createdb or createrole),
                f"current_user={role} super={superuser} createdb={createdb} createrole={createrole}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        results.append(
            Check("rol de la aplicación sin privilegios de administración", False,
                  f"{type(exc).__name__}: {exc}")
        )

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Comprueba la conectividad y el esquema vectorial de Supabase."
    )
    parser.add_argument("--database-url-file", default="", help="fichero con la DSN (modo 600)")
    parser.add_argument("--database-url", default="", help="DSN (evítalo: queda en el historial)")
    parser.add_argument("--expected-extension-schema", default=SUPABASE_EXTENSION_SCHEMA)
    args = parser.parse_args(argv)

    dsn = args.database_url
    if args.database_url_file:
        path = pathlib.Path(args.database_url_file)
        if not path.is_file():
            print(f"error: no existe {path}", file=sys.stderr)
            return 2
        dsn = path.read_text(encoding="utf-8").strip()
    if not dsn:
        print("error: hace falta --database-url-file (la DSN nunca va en argv)", file=sys.stderr)
        return 2

    import sqlalchemy as sa

    try:
        engine = sa.create_engine(dsn)
    except Exception as exc:  # noqa: BLE001 - a malformed DSN cannot be checked
        print(f"error: la DSN no se puede interpretar: {type(exc).__name__}", file=sys.stderr)
        return 2

    try:
        with engine.connect() as connection:
            results = checks(connection, expected_schema=args.expected_extension_schema)
    except Exception as exc:  # noqa: BLE001 - an unreachable database is exit 2, not a finding
        print(f"error: no se pudo conectar: {type(exc).__name__}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()

    for check in results:
        print(f"  [{'ok' if check.ok else 'FALLA'}] {check.name}: {check.detail}")

    failed = [check.name for check in results if not check.ok]
    if failed:
        print(
            f"\n{len(failed)} comprobación(es) fallan: {', '.join(failed)}.\n"
            "Si el fallo es el esquema de vector: en un proyecto Supabase nuevo hay que ejecutar\n"
            "  grant usage on schema extensions to taa_app;\n"
            "  alter database postgres set search_path = public, extensions;\n"
            "y reconectar (el search_path de base de datos se aplica a las conexiones nuevas).",
            file=sys.stderr,
        )
        return 1
    print("\nok: conectividad, pgvector y resolución del tipo comprobados")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
