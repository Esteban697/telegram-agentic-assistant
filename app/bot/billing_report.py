"""Render the billing rows as the Telegram digest (Spanish lives in the catalogue)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.bot import messages


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


__all__ = ["compose_report"]
