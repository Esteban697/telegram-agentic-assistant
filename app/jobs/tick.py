"""Consolidated scheduled dispatcher.

One Cloud Run Job container start handles all recurring work, instead of one container per
queue. Reminders run on every 15-minute tick; resume and document work run on their own
sub-intervals inside that same process.

    python -m app.jobs.tick

The billing digest is deliberately not folded in here: it runs once a day and has different
failure semantics from the queues.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config import load_settings
from app.jobs import dispatch_reminders, process_documents, resume_work, report_line
from app.logging_config import configure_logging


def selected_jobs(local_now: datetime) -> tuple[str, ...]:
    """Return the queue jobs due at this local time.

    The scheduler fires every 15 minutes. Reminders are the user-visible cadence; resume is a
    recovery path and documents can wait for the top of the hour. Keep this pure so the timing
    rules are testable without starting a container.
    """

    jobs = ["reminders"]
    if local_now.minute in (0, 30):
        jobs.append("resume")
    if local_now.minute == 0:
        jobs.append("documents")
    return tuple(jobs)


async def run(now: datetime | None = None) -> dict[str, Any]:
    settings = load_settings().validate_or_raise()
    configure_logging(
        level=settings.log_level,
        app_env=settings.app_env,
        content_debug=settings.log_content_debug,
    )

    local_now = (now or datetime.now(UTC)).astimezone(ZoneInfo(settings.default_timezone))
    names = selected_jobs(local_now)
    modules = {
        "reminders": dispatch_reminders,
        "resume": resume_work,
        "documents": process_documents,
    }

    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name in names:
        try:
            results[name] = await modules[name].run()
        except Exception as exc:  # noqa: BLE001 - one queue must not stop the others
            errors[name] = f"{type(exc).__name__}: {str(exc)[:160]}"

    return {
        "at": local_now.isoformat(),
        "jobs": list(names),
        "results": results,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    from app.storage.postgres import use_compatible_event_loop

    use_compatible_event_loop()
    try:
        payload = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - a job exits non-zero with a clear reason
        print(report_line({"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]}))
        return 1

    ok = not payload["errors"]
    print(report_line({"ok": ok, **payload}))
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
