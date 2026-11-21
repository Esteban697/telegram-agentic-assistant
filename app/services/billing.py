"""Cloud Billing BigQuery reader.

Layer 3. The daily job and the `/gastos` command/tool share this service: one place parses the
export table, runs the query and returns structured rows. The export itself is a one-time Console
toggle; this module only reads it, and returns ``None`` when the table is not there yet.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings

_logger = logging.getLogger(__name__)

BIGQUERY_SCOPES = ["https://www.googleapis.com/auth/bigquery"]
_RESOURCE_PREFIX = "gcp_billing_export_resource_v1_"
_STANDARD_PREFIX = "gcp_billing_export_v1_"


def build_query(*, project: str, dataset: str, table: str, days: int) -> str:
    """Daily net cost by project/service, including credits."""

    credit_sum = "SUM(IFNULL((SELECT SUM(credit.amount) FROM UNNEST(credits) AS credit), 0))"
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
          AND DATE(usage_start_time) < CURRENT_DATE()
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


def _table_ids(payload: dict[str, Any]) -> list[str]:
    """`tables.list` puts the id in `tableReference.tableId`; tolerate the flat form too."""

    ids: list[str] = []
    for table in payload.get("tables", []):
        reference = table.get("tableReference") or {}
        table_id = reference.get("tableId") or table.get("tableId")
        if table_id:
            ids.append(str(table_id))
    return ids


def _access_token() -> str:
    import google.auth
    from google.auth.transport.requests import Request

    credentials, _project = google.auth.default(scopes=BIGQUERY_SCOPES)
    credentials.refresh(Request())
    return str(credentials.token)


@dataclass(frozen=True, slots=True)
class BillingRows:
    rows: list[dict[str, Any]]
    table: str


class BigQueryBillingClient:
    """The two REST calls the report needs: list tables and run one query."""

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
        table_ids = _table_ids(response.json())
        return next(
            (table for table in table_ids if table.startswith(_RESOURCE_PREFIX)),
            next((table for table in table_ids if table.startswith(_STANDARD_PREFIX)), None),
        )

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


class BillingService:
    """Read the billing export for the configured project."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.project = settings.billing_export_project or settings.google_cloud_project

    async def fetch_rows(self, *, days: int | None = None) -> BillingRows | None:
        """Return the rows, or ``None`` when the export table is not available yet."""

        if not self.project:
            return None
        token = await asyncio.to_thread(_access_token)
        async with httpx.AsyncClient(timeout=60.0) as client:
            billing = BigQueryBillingClient(
                project=self.project, token=token, client=client
            )
            table = await billing.resolve_table(
                dataset=self.settings.billing_export_dataset,
                explicit=self.settings.billing_export_table,
            )
            if table is None:
                _logger.warning(
                    "billing export table is missing",
                    extra={
                        "status": "export_missing",
                        "dataset": self.settings.billing_export_dataset,
                    },
                )
                return None
            rows = await billing.query(
                build_query(
                    project=self.project,
                    dataset=self.settings.billing_export_dataset,
                    table=table,
                    days=days or self.settings.billing_report_days,
                )
            )
            return BillingRows(rows=rows, table=table)


__all__ = [
    "BIGQUERY_SCOPES",
    "BigQueryBillingClient",
    "BillingRows",
    "BillingService",
    "build_query",
    "parse_query_result",
]
