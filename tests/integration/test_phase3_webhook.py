"""Criterion (h) at the webhook layer: the inbox records both updates exactly once.

The handler-level test asserts the declination (no second graph run, one busy reply, one
watchdog row). This one drives the **real webhook** so the other half of (h) — "still recorded
exactly once by the inbox path" — is asserted where the inbox is actually written.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from app.bot import messages
from app.llm.protocols import ChatResult
from app.main import create_app
from tests.conftest import (
    CHAT_ID,
    TEST_DATABASE_URL,
    USER_ID,
    WEBHOOK_SECRET,
    RecordingTelegram,
    make_db_settings,
    truncate_all,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set"),
]


class GatedModel:
    """Answers only when the test opens the gate.

    The first version slept for 1.5 s and assumed the second request would arrive inside that
    window. Under load it sometimes did not: the first turn finished, the second message was
    served normally, and the test failed on `model.calls == 1` — a test artifact, not a product
    race. The gate makes "the second message arrived mid-turn" a fact the test *establishes*
    (it posts the second message only after `entered` is set) instead of a stopwatch guess.
    """

    model = "gated-model"

    def __init__(self) -> None:
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def chat(self, messages_, **_: Any) -> ChatResult:  # noqa: ANN001
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return ChatResult(content="primera respuesta", model=self.model, latency_ms=1.0)

    async def list_models(self) -> list[str]:
        return [self.model]


def _update(update_id: int, text: str) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": USER_ID, "is_bot": False, "first_name": "U"},
            "chat": {"id": CHAT_ID, "type": "private"},
            "date": 1_700_000_000,
            "text": text,
        },
    }


async def test_h_the_webhook_declines_the_second_message_and_records_both_once() -> None:
    settings = make_db_settings()
    truncate_all(settings)
    model = GatedModel()
    telegram = RecordingTelegram()
    app = create_app(settings=settings, chat_model=model, telegram_client=telegram)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            headers = {"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET}
            # The first turn is held open inside the model, so the second message is provably
            # concurrent rather than probably concurrent.
            first_task = asyncio.create_task(
                client.post("/telegram/webhook", json=_update(1, "primer mensaje"), headers=headers)
            )
            await asyncio.wait_for(model.entered.wait(), timeout=30.0)
            second = await client.post(
                "/telegram/webhook", json=_update(2, "segundo mensaje"), headers=headers
            )
            model.release.set()
            first = await asyncio.wait_for(first_task, timeout=30.0)

            engine = sa.create_engine(settings.database_sync_url.get_secret_value())
            try:
                with engine.connect() as connection:
                    inbox = connection.execute(
                        sa.text("SELECT update_id, status FROM inbox_updates ORDER BY update_id")
                    ).all()
                    items = connection.execute(
                        sa.text("SELECT state FROM work_items")
                    ).all()
            finally:
                engine.dispose()

    assert first.status_code == second.status_code == 200

    # The declination really happened, so this test cannot pass without it.
    assert model.calls == 1, "the declined message must not reach the model"
    assert telegram.sent_texts.count(messages.render("busy.previous_turn_in_progress")) == 1

    # Both updates were recorded, each exactly once — the inbox path was not weakened.
    assert [row[0] for row in inbox] == [1, 2], f"the inbox must hold both updates: {inbox}"
    assert len({row[0] for row in inbox}) == len(inbox)

    # One watchdog row for the thread, and it is closed: no orphaned turn.
    assert len(items) == 1, f"no second watchdog row: {items}"
    assert [row[0] for row in items] == ["done"]
