"""Daily Google Cloud cost digest from the BigQuery billing export.

The export itself is a one-time Console toggle (there is no public API for it). This job only
reads the export and sends a compact Telegram message: the latest complete day, the assistant
project's total, its top services, the whole billing account total, and month-to-date for the
assistant. It is silent while the export table does not exist yet.

    python -m app.jobs.billing_report
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.bot import messages
from app.bot.telegram_client import TelegramClient
from app.config import load_settings
from app.jobs import report_line
from app.logging_config import configure_logging
from app.storage.postgres import use_compatible_event_loop

_logger = logging.getLogger(__name__)

BIGQUERY_SCOPES = ["https://www.googleapis.com/auth/bigquery"]
_RESOURCE_PREFIX = "gcp_billing_export_resource_v1_"
_STANDARD_PREFIX = "gcp_billing_export_v1_"


def build_query(*, project: str, dataset: str, table: str, days: int) -> str:
    """Daily net cost by project/service, including credits."""

    credit_sum = (
        "SUM(IFNULL((SELECT SUM(credit.amount) FROM UNNEST(credits) AS credit), 0))"
    )
    return f"""
        SELECT
          FORMAT_DATE('%Y-%m-%d', DATE(usage_start_time)) AS day,
          project.id AS project_id,
          service.description AS service,
          currency,
          SUM(cost) AS gross,
          {credit_sum} AS credits,
          SUM(cost) + {credit_sum} AS net
        FROM `{project}.{dataset}.{table}`
        WHERE DATE(usage_start_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL {days} DAY)
        GROUP BY day, project_id, service, currency
        ORDER BY day DESC, net DESC
    """


def parse_query_result(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert BigQuery's `{schema, rows}` response into ordinary dictionaries."""

    schema = payload.get("schema") or {}
    fields = [field.get("name") for field in schema.get("fields", [])]
    parsed: list[dict[str, Any]] = []
    for row in payload.get("rows", []):
        values = [cell.get("v") for cell in row.get("f", [])]
        parsed.append(dict(zip(fields, values)))
    return parsed


def compose_report(
    rows: list[dict[str, Any]],
    *,
    assistant_project: str,
    month: str | None = None,
) -> str:
    """Build the Telegram message from the query rows."""

    if not rows:
        return messages.render("billing.report.empty")

    latest_day = max(str(row["day"]) for row in rows)
    latest = [row for row in rows if str(row["day"]) == latest_day]
    assistant = [row for row in latest if str(row["project_id"]) == assistant_project]
    currency = str((assistant or latest)[0].get("currency") or "EUR")

    def net(row: dict[str, Any]) -> float:
        try:
            return float(row.get("net") or 0)
        except (TypeError, ValueError):
            return 0.0

    assistant_total = sum(net(row) for row in assistant)
    account_total = sum(net(row) for row in latest)

    by_service: dict[str, float] = {}
    for row in assistant:
        service = str(row.get("service") or "otros")
        by_service[service] = by_service.get(service, 0.0) + net(row)
    top = sorted(by_service.items(), key=lambda item: item[1], reverse=True)[:3]

    current_month = month or datetime.now(UTC).strftime("%Y-%m")
    month_total = sum(
        net(row)
        for row in rows
        if str(row["project_id"]) == assistant_project
        and str(row["day"]).startswith(current_month)
    )

    lines = [messages.render("billing.report.header", day=latest_day)]
    if assistant:
        lines.append(
            messages.render(
                "billing.report.assistant_line",
                project=assistant_project,
                amount=f"{assistant_total:.2f}",
                currency=currency,
            )
        )
        for service, amount in top:
            lines.append(
                messages.render(
                    "billing.report.service_line",
                    service=service,
                    amount=f"{amount:.2f}",
                    currency=currency,
                )
            )
    lines.append(
        messages.render(
            "billing.report.account_line",
            amount=f"{account_total:.2f}",
            currency=currency,
        )
    )
    lines.append(
        messages.render(
            "billing.report.month_line",
            amount=f"{month_total:.2f}",
            currency=currency,
        )
    )
    return "\n".join(lines)


def _access_token() -> str:
    import google.auth
    from google.auth.transport.requests import Request

    credentials, _project = google.auth.default(scopes=BIGQUERY_SCOPES)
    credentials.refresh(Request())
    return str(credentials.token)


class BigQueryBillingClient:
    """The two REST calls the digest needs: list tables and run one query."""

    def __init__(
        self,
        *,
        project: str,
        token: str,
        client: httpx.AsyncClient,
    ) -> None:
        self._project = project
        self._token = token
        self._client = client

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    async def resolve_table(self, *, dataset: str, explicit: str) -> str | None:
        if explicit.strip():
            return explicit.strip()
        response = await self._client.get(
            f"https://bigquery.googleapis.com/bigquery/v2/projects/{self._project}"
            f"/datasets/{dataset}/tables",
            headers=self._headers,
            params={"maxResults": 1000},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        table_ids = [
            str(table.get("tableId") or "") for table in response.json().get("tables", [])
        ]
        resource = next(
            (table for table in table_ids if table.startswith(_RESOURCE_PREFIX)), None
        )
        standard = next(
            (table for table in table_ids if table.startswith(_STANDARD_PREFIX)), None
        )
        return resource or standard

    async def query(self, sql: str) -> list[dict[str, Any]]:
        response = await self._client.post(
            f"https://bigquery.googleapis.com/bigquery/v2/projects/{self._project}/queries",
            headers=self._headers,
            json={"query": sql, "useLegacySql": False, "timeoutMs": 60000},
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("jobComplete", False):
            job_id = str((payload.get("jobReference") or {}).get("jobId") or "")
            if not job_id:
                raise RuntimeError("BigQuery query did not complete and returned no job id")
            for _ in range(20):
                await asyncio.sleep(0.5)
                poll = await self._client.get(
                    f"https://bigquery.googleapis.com/bigquery/v2/projects/{self._project}"
                    f"/queries/{job_id}",
                    headers=self._headers,
                )
                poll.raise_for_status()
                payload = poll.json()
                if payload.get("jobComplete", False):
                    break
            else:
                raise RuntimeError("BigQuery query did not finish in time")
        return parse_query_result(payload)


async def run() -> dict[str, Any]:
    settings = load_settings().validate_or_raise()
    configure_logging(
        level=settings.log_level,
        app_env=settings.app_env,
        content_debug=settings.log_content_debug,
    )

    project = settings.billing_export_project or settings.google_cloud_project
    if not project:
        return {"status": "no_project"}
    chat_id = next(iter(settings.telegram_allowed_user_ids), None)
    if chat_id is None:
        return {"status": "no_chat_id"}

    token = _access_token()
    async with httpx.AsyncClient(timeout=60.0) as client:
        billing = BigQueryBillingClient(project=project, token=token, client=client)
        table = await billing.resolve_table(
            dataset=settings.billing_export_dataset,
            explicit=settings.billing_export_table,
        )
        if table is None:
            # The one-time Console export has not been enabled yet. Stay silent: the scheduler
            # should not send a daily setup reminder.
            _logger.warning(
                "billing export table is missing",
                extra={"status": "export_missing", "dataset": settings.billing_export_dataset},
            )
            return {"status": "export_missing"}
        rows = await billing.query(
            build_query(
                project=project,
                dataset=settings.billing_export_dataset,
                table=table,
                days=settings.billing_report_days,
            )
        )

    text = compose_report(rows, assistant_project=project)
    telegram = TelegramClient(
        token=settings.bot_token, base_url=settings.telegram_api_base_url
    )
    try:
        await telegram.send_message(chat_id=chat_id, text=text)
    finally:
        await telegram.aclose()
    latest_day = max((str(row.get("day")) for row in rows), default=None)
    return {"status": "sent", "rows": len(rows), "latest_day": latest_day}


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
