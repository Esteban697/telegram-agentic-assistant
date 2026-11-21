"""Billing digest helpers: query shape, BigQuery parsing, message composition."""

from __future__ import annotations

import httpx
import pytest

from app.bot.billing_report import compose_report
from app.services.billing import (
    BigQueryBillingClient,
    build_query,
    parse_query_result,
)


def test_query_selects_daily_net_cost_and_credits() -> None:
    sql = build_query(project="p", dataset="billing_export", table="gcp_billing_export_v1_x", days=8)

    assert "`p.billing_export.gcp_billing_export_v1_x`" in sql
    assert "FORMAT_DATE('%Y-%m-%d', DATE(usage_start_time))" in sql
    assert "UNNEST(credits)" in sql
    assert "INTERVAL 8 DAY" in sql
    assert "DATE(usage_start_time) < CURRENT_DATE()" in sql


def test_parse_query_result_maps_schema_fields() -> None:
    payload = {
        "schema": {
            "fields": [
                {"name": "day"},
                {"name": "project_id"},
                {"name": "service"},
                {"name": "currency"},
                {"name": "net"},
            ]
        },
        "rows": [
            {
                "f": [
                    {"v": "2026-11-05"},
                    {"v": "telegram-assistant-esteban"},
                    {"v": "Cloud Run"},
                    {"v": "EUR"},
                    {"v": "0.12"},
                ]
            }
        ],
    }
    assert parse_query_result(payload) == [
        {
            "day": "2026-11-05",
            "project_id": "telegram-assistant-esteban",
            "service": "Cloud Run",
            "currency": "EUR",
            "net": "0.12",
        }
    ]


def test_compose_report_includes_assistant_top_services_and_account_total() -> None:
    rows = [
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
        {
            "day": "2026-11-05",
            "project_id": "portfolio-web-chatbot",
            "service": "Cloud Run",
            "currency": "EUR",
            "net": "0.05",
        },
        {
            "day": "2026-11-01",
            "project_id": "telegram-assistant-esteban",
            "service": "Cloud Run",
            "currency": "EUR",
            "net": "1.00",
        },
    ]

    text = compose_report(
        rows, assistant_project="telegram-assistant-esteban", month="2026-11"
    )

    assert "2026-11-05" in text
    assert "día completo" in text
    assert "Asistente (telegram-assistant-esteban): 1.000 EUR" in text
    assert "Cloud Run: 0.800 EUR" in text
    assert "Cloud Logging: 0.200 EUR" in text
    assert "Total de la cuenta: 1.050 EUR" in text
    assert "Mes en curso (asistente): 2.000 EUR" in text


def test_compose_report_with_no_rows_is_explicit() -> None:
    assert "No hay datos" in compose_report([], assistant_project="p")


async def test_resolve_table_prefers_the_detailed_export() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "tables": [
                    {
                        "tableReference": {
                            "tableId": "gcp_billing_export_v1_abc"
                        }
                    },
                    {
                        "tableReference": {
                            "tableId": "gcp_billing_export_resource_v1_abc"
                        }
                    },
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    billing = BigQueryBillingClient(project="p", token="t", client=client)
    try:
        table = await billing.resolve_table(dataset="billing_export", explicit="")
    finally:
        await client.aclose()
    assert table == "gcp_billing_export_resource_v1_abc"


async def test_resolve_table_returns_none_when_the_export_is_missing() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    billing = BigQueryBillingClient(project="p", token="t", client=client)
    try:
        table = await billing.resolve_table(dataset="billing_export", explicit="")
    finally:
        await client.aclose()
    assert table is None


async def test_an_explicit_table_wins_without_a_request() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("an explicit table must not trigger a request")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    billing = BigQueryBillingClient(project="p", token="t", client=client)
    try:
        table = await billing.resolve_table(
            dataset="billing_export", explicit="gcp_billing_export_v1_explicit"
        )
    finally:
        await client.aclose()
    assert table == "gcp_billing_export_v1_explicit"
