"""Health endpoints (PLAN.md §8.2).

Neither endpoint reveals version strings, DSNs, bucket names, counts or internal hostnames
(spec §13: "endpoints de salud que no revelen secretos ni información interna").

**Phase 1 semantics (RN12):** with no database in Phase 1, ``/readyz`` reports ready on
*settings validation alone*. The database, ``vector``-extension and active-embedding-model
checks are added in Phases 2 and 4, so the endpoint's contract grows rather than being
undefined.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is up. No dependencies touched, always cheap."""

    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """Readiness: configuration validated and the app finished starting.

    Deliberately terse. In Phase 1 the only dependency is the validated configuration, which
    the lifespan already checked; a failure here means the process started with a state it
    should not have.
    """

    handlers = getattr(request.app.state, "handlers", None)
    if handlers is None:
        return JSONResponse(status_code=503, content={"status": "not_ready"})
    return JSONResponse(status_code=200, content={"status": "ready"})


__all__ = ["router"]
