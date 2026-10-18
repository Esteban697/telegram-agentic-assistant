"""Health endpoints (PLAN.md §8.2, RN12).

Neither endpoint reveals version strings, DSNs, bucket names, counts or internal hostnames
(spec §13: "endpoints de salud que no revelen secretos ni información interna").

**Readiness contract.** RN12 grows the endpoint with the phases, and §8.2's failure matrix
depends on it: when PostgreSQL is unreachable, Telegram must be told not to deliver, so this
endpoint has to report not-ready rather than a cheerful `ready`. When the configured store is
PostgreSQL, readiness therefore requires, in order:

1. validated settings (the lifespan already checked them, and the handlers are wired);
2. the database reachable;
3. the ``vector`` extension installed at **>= 0.8** (`hnsw.iterative_scan` is a 0.8 feature, and
   below it a filtered vector search silently returns too few rows);
4. exactly one readable active ``embedding_models`` row — the single authority retrieval reads.

With the in-memory store there is no database to check, so only (1) applies; that is a test and
local-development configuration, not the deployed one.
"""

from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.storage import repositories

router = APIRouter()

MINIMUM_VECTOR_VERSION = (0, 8)


def _not_ready(reason: str) -> JSONResponse:
    """Terse on purpose: the reason is a fixed token, never a value from the environment."""

    return JSONResponse(status_code=503, content={"status": "not_ready", "reason": reason})


def _below_minimum(version: str) -> bool:
    parts: list[int] = []
    for part in str(version).split(".")[:2]:
        digits = "".join(character for character in part if character.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts) < MINIMUM_VECTOR_VERSION


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is up. No dependencies touched, always cheap."""

    return {"status": "ok"}


# **Cloud Run's Google Frontend intercepts paths ending in `z`** (Cloud Run known issues: avoid all
# such paths). `/healthz` therefore never reaches the container on Cloud Run — the frontend answers
# with its own HTML 404 — while it works locally and in the image. `/health` is the deployable
# liveness path; the `healthz` spelling is kept for local use and for everything already documented.
@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness for Cloud Run, where paths ending in `z` are reserved by the frontend."""

    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """Readiness: settings validated, and every dependency this configuration needs."""

    state = request.app.state
    if getattr(state, "handlers", None) is None:
        return _not_ready("startup")

    settings = getattr(state, "settings", None)
    if settings is not None and settings.store_backend != "postgres":
        return JSONResponse(status_code=200, content={"status": "ready"})

    database = getattr(state, "database", None)
    if database is None:
        return _not_ready("database")

    try:
        async with database.sessions() as session:
            installed = (
                await session.execute(
                    sa.text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                )
            ).scalar_one_or_none()
            if installed is None or _below_minimum(str(installed)):
                return _not_ready("vector_extension")
            active = await repositories.get_active_embedding_model(session)
    except Exception:  # noqa: BLE001 - any failure here means "cannot serve", which is the answer
        return _not_ready("database")

    if active is None:
        return _not_ready("embedding_model")
    return JSONResponse(status_code=200, content={"status": "ready"})


__all__ = ["MINIMUM_VECTOR_VERSION", "router"]
