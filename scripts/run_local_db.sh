#!/usr/bin/env bash
# Local PostgreSQL 17 + pgvector for development and the DB-backed test suite.
#
#   bash scripts/run_local_db.sh          # start (idempotent)
#   bash scripts/run_local_db.sh stop     # stop and remove
#   bash scripts/run_local_db.sh psql     # open a shell
#
# On Windows the same container can be started from PowerShell with:
#   docker run -d --name taa-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=taa `
#     -p 55432:5432 pgvector/pgvector:pg17
#
# Then, in .env:
#   DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:55432/taa
#   DATABASE_SYNC_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:55432/taa
#
# Port 55432 avoids clashing with a local PostgreSQL install on 5432.
set -euo pipefail

NAME="${TAA_PG_NAME:-taa-pg}"
PORT="${TAA_PG_PORT:-55432}"
IMAGE="pgvector/pgvector:pg17"

case "${1:-start}" in
  start)
    if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
      docker start "$NAME" >/dev/null
    else
      docker run -d --name "$NAME" \
        -e POSTGRES_PASSWORD=postgres \
        -e POSTGRES_DB=taa \
        -p "${PORT}:5432" \
        "$IMAGE" >/dev/null
    fi
    printf 'waiting for %s on port %s' "$NAME" "$PORT"
    for _ in $(seq 1 30); do
      if docker exec "$NAME" pg_isready -U postgres -d taa >/dev/null 2>&1; then
        printf '\nready: postgresql+psycopg://postgres:postgres@127.0.0.1:%s/taa\n' "$PORT"
        exit 0
      fi
      printf '.'
      sleep 1
    done
    printf '\ntimed out waiting for PostgreSQL\n' >&2
    exit 1
    ;;
  stop)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    echo "removed $NAME"
    ;;
  psql)
    shift || true
    docker exec -it "$NAME" psql -U postgres -d taa "$@"
    ;;
  *)
    echo "usage: $0 [start|stop|psql]" >&2
    exit 2
    ;;
esac
