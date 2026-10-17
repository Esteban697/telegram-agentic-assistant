"""Reminder dispatch job (PLAN.md §5.2, §11.3).

Built and tested in Phase 2; **not scheduled** until Phase 5 creates the Cloud Run Job and the
Cloud Scheduler trigger. Running it by hand is the Phase 2 demonstration:

    python -m app.jobs.dispatch_reminders

It prints one `JOB_REPORT {...}` line with the counts and the per-task outcomes, which is what
the concurrency acceptance criterion reads from outside the process.
"""

from __future__ import annotations

import asyncio
import sys
import zoneinfo

from app.bot import messages
from app.bot.telegram_client import TelegramClient
from app.config import load_settings
from app.jobs import executor_id, report_line
from app.logging_config import configure_logging
from app.services.notifications import build_notifier
from app.services.reminders import ReminderDispatcher
from app.storage.postgres import build_database, use_compatible_event_loop


def _reminder_text(task) -> str:  # noqa: ANN001
    """Render the reminder from the Spanish catalogue, in the task's own timezone.

    Injected here because the dispatcher (a service) may not import `app.bot.messages`
    (PLAN.md §2.2); the job sits above the transport layer and may.
    """

    try:
        due = task.due_at.astimezone(zoneinfo.ZoneInfo(task.timezone))
        when = due.strftime("%d/%m/%Y %H:%M")
    except Exception:  # pragma: no cover - a bad zone is a data problem, not a crash
        when = task.due_at.isoformat()
    return messages.render("reminder.due", title=task.title, when=when)


async def run() -> dict[str, object]:
    settings = load_settings().validate_or_raise()
    configure_logging(
        level=settings.log_level,
        app_env=settings.app_env,
        content_debug=settings.log_content_debug,
    )

    database = build_database(settings)
    telegram = TelegramClient(token=settings.bot_token, base_url=settings.telegram_api_base_url)
    notifier = build_notifier(settings, telegram)

    dispatcher = ReminderDispatcher(
        sessions=database.sessions,
        notifier=notifier,
        executor=executor_id("reminders"),
        batch_size=settings.reminder_batch_size,
        lease_seconds=settings.dispatcher_lease_seconds,
        max_attempts=settings.reminder_max_attempts,
        text_renderer=_reminder_text,
    )
    try:
        report = await dispatcher.run_once()
        return report.as_dict()
    finally:
        await telegram.aclose()
        await database.dispose()


def main(argv: list[str] | None = None) -> int:
    # Before asyncio.run creates the loop (Windows: psycopg async needs the selector loop).
    use_compatible_event_loop()
    try:
        payload = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - a job must exit non-zero with a clear reason
        print(report_line({"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]}))
        return 1
    print(report_line({"ok": True, **payload}))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
