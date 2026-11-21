"""`/gastos` command and the get_cloud_costs tool, without BigQuery."""

from __future__ import annotations

from typing import Any

from app.bot.handlers import TelegramHandlers
from app.policy.authz import TelegramIdentity
from app.services.billing import BillingRows
from app.tools.billing import BillingTool, GetCostsArgs
from tests.conftest import RecordingTelegram, make_settings

ROWS = [
    {
        "day": "2026-11-05",
        "project_id": "telegram-assistant-esteban",
        "service": "Cloud Run",
        "currency": "EUR",
        "net": "0.80",
    },
    {
        "day": "2026-11-05",
        "project_id": "telegram-assistant-esteban",
        "service": "Cloud Logging",
        "currency": "EUR",
        "net": "0.20",
    },
]


class FakeBilling:
    project = "telegram-assistant-esteban"

    def __init__(self, rows: list[dict[str, Any]] | None) -> None:
        self.rows = rows

    async def fetch_rows(self, *, days: int | None = None) -> BillingRows | None:
        return None if self.rows is None else BillingRows(rows=self.rows, table="t")


async def test_the_gastos_command_renders_the_report() -> None:
    telegram = RecordingTelegram()
    handlers = TelegramHandlers(
        settings=make_settings(),
        telegram=telegram,  # type: ignore[arg-type]
        agent=None,  # type: ignore[arg-type]
        conversations=None,  # type: ignore[arg-type]
        billing_service=FakeBilling(ROWS),
    )
    update = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "from": {"id": 42},
            "chat": {"id": 4242},
            "text": "/gastos",
        },
    }

    outcome = await handlers.handle(update, TelegramIdentity(user_id=42, chat_id=4242))

    assert outcome.detail == "billing_report"
    text = telegram.calls[-1].kwargs["text"]
    assert "Gastos de Google Cloud" in text
    assert "Asistente (telegram-assistant-esteban): 1.000 EUR" in text


async def test_the_gastos_command_explains_a_missing_export() -> None:
    telegram = RecordingTelegram()
    handlers = TelegramHandlers(
        settings=make_settings(),
        telegram=telegram,  # type: ignore[arg-type]
        agent=None,  # type: ignore[arg-type]
        conversations=None,  # type: ignore[arg-type]
        billing_service=FakeBilling(None),
    )
    update = {
        "update_id": 2,
        "message": {
            "message_id": 2,
            "from": {"id": 42},
            "chat": {"id": 4242},
            "text": "/gastos",
        },
    }

    outcome = await handlers.handle(update, TelegramIdentity(user_id=42, chat_id=4242))

    assert outcome.detail == "billing_export_missing"
    assert "exportación de facturación" in telegram.calls[-1].kwargs["text"]


async def test_the_cost_tool_returns_rows_to_the_model() -> None:
    result = await BillingTool(service=FakeBilling(ROWS)).get_cloud_costs(
        user_id=1, args=GetCostsArgs(days=7)
    )

    assert result.ok is True
    assert result.data["assistant_project"] == "telegram-assistant-esteban"
    assert len(result.data["rows"]) == 2


async def test_the_cost_tool_reports_a_missing_export() -> None:
    result = await BillingTool(service=FakeBilling(None)).get_cloud_costs(
        user_id=1, args=GetCostsArgs(days=7)
    )

    assert result.ok is False
    assert result.error_code == "billing_export_missing"
