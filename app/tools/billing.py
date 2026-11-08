"""Read-only cloud cost tool (level 0).

The chat model has no clock-like knowledge of spend; this tool gives it the rows from the Cloud
Billing BigQuery export so it can answer "¿cuánto he gastado?" with real figures instead of
searching the web. It only reads; no approval is needed or possible.

Layer 4. The BigQuery work is in ``app.services.billing``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from app.policy.risk import RiskLevel
from app.services.billing import BillingService
from app.tools.base import ToolResult
from app.tools.registry import ToolSpec

_logger = logging.getLogger(__name__)


class GetCostsArgs(BaseModel):
    days: int = Field(
        default=7,
        ge=1,
        le=31,
        description="How many days of history to return, ending today (UTC).",
    )


@dataclass
class BillingTool:
    service: BillingService | None = None

    async def get_cloud_costs(
        self, *, user_id: int, args: GetCostsArgs | None = None, **_: Any
    ) -> ToolResult[dict[str, Any]]:
        if self.service is None:
            return ToolResult.failure(
                "CapabilityUnavailable",
                message_key="error.capability_unavailable",
                detail="the billing export is not configured",
                capability="get_cloud_costs",
            )
        days = (args or GetCostsArgs()).days
        try:
            snapshot = await self.service.fetch_rows(days=days)
        except Exception as exc:  # noqa: BLE001 - provider failure becomes a typed result
            _logger.warning(
                "billing query failed",
                extra={"error_code": "billing_unavailable", "error_type": type(exc).__name__},
            )
            return ToolResult.failure(
                "billing_unavailable",
                message_key="billing.report.unavailable",
                detail="the BigQuery billing query failed",
            )
        if snapshot is None:
            return ToolResult.failure(
                "billing_export_missing",
                message_key="billing.export_missing",
                detail="the billing export table does not exist yet",
            )
        rows = snapshot.rows[:200]
        latest_day = max((str(row.get("day")) for row in rows), default=None)
        return ToolResult.success(
            {
                "assistant_project": self.service.project,
                "days": days,
                "latest_day": latest_day,
                "rows": rows,
            }
        )


def specs(tool: BillingTool) -> list[ToolSpec]:
    return [
        ToolSpec(
            name="get_cloud_costs",
            description=(
                "Read Google Cloud costs from the billing BigQuery export. Returns daily net "
                "cost by project and service for the last N days, plus the assistant project. "
                "Use it for any question about spend; do not search the web for prices."
            ),
            args_model=GetCostsArgs,
            risk_level=RiskLevel.READ,
            timeout_seconds=30.0,
            handler=tool.get_cloud_costs,
        )
    ]


__all__ = ["BillingTool", "GetCostsArgs", "specs"]
