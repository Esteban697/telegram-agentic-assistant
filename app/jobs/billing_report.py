"""Daily Google Cloud cost digest from the BigQuery billing export.

The export itself is a one-time Console toggle (there is no public API for it). This job reads the
export through :class:`app.services.billing.BillingService` and sends the rendered digest; it is
silent while the export table does not exist yet.

    python -m app.jobs.billing_report
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.bot.billing_report import compose_report
from app.bot.telegram_client import TelegramClient
from app.config import load_settings
from app.jobs import report_line
from app.logging_config import configure_logging
from app.services.billing import BillingService
from app.storage.postgres import use_compatible_event_loop

_logger = logging.getLogger(__name__)


async def run() -> dict[str, Any]:
    settings = load_settings().validate_or_raise()
    configure_logging(
        level=settings.log_level,
        app_env=settings.app_env,
        content_debug=settings.log_content_debug,
    )

    chat_id = next(iter(settings.telegram_allowed_user_ids), None)
    if chat_id is None:
        return {"status": "no_chat_id"}

    billing = BillingService(settings)
    snapshot = await billing.fetch_rows()
    if snapshot is None:
        # The one-time Console export has not produced a table yet. Stay silent: the scheduler
        # should not send a daily setup reminder.
        return {"status": "export_missing"}

    text = compose_report(snapshot.rows, assistant_project=billing.project)
    telegram = TelegramClient(
        token=settings.bot_token, base_url=settings.telegram_api_base_url
    )
    try:
        await telegram.send_message(chat_id=chat_id, text=text)
    finally:
        await telegram.aclose()
    latest_day = max((str(row.get("day")) for row in snapshot.rows), default=None)
    return {
        "status": "sent",
        "rows": len(snapshot.rows),
        "latest_day": latest_day,
        "table": snapshot.table,
    }


def main() -> int:
    use_compatible_event_loop()
    try:
        report = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - a job reports, it does not crash silently
        _logger.exception("billing report failed", extra={"status": "failed"})
        print(report_line({"status": "failed", "error": type(exc).__name__}))
        return 1
    print(report_line(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
