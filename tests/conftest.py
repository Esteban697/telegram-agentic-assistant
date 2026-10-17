"""Shared fixtures.

Two principles, both from PLAN.md §9:

* **No credentials and no network.** Every transport is mocked or replaced, so the whole
  suite runs on a clean checkout with no ``.env`` (A1). ``httpx.MockTransport`` is used where
  the *real* client's serialisation must be exercised (the outbound request body, the
  ``ok:false`` envelope), and a recording double where the assertion is about *how many*
  calls were made.
* **Determinism.** No test reads the clock: TTL behaviour is driven through an injected
  ``clock`` callable, and no test sleeps.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from app.bot.telegram_client import DownloadedFile, SentMessage
from app.config import Settings
from app.llm.ollama_client import OllamaChatClient
from app.storage.postgres import use_compatible_event_loop

# Windows: psycopg's async mode cannot run on the default ProactorEventLoop, so the policy must
# be installed before pytest-asyncio creates any loop. No-op on Linux.
use_compatible_event_loop()

# The DSN the DB-backed tests use. Unset => those tests skip with an explicit reason.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

CHAT_ID = 4242
USER_ID = 42
WEBHOOK_SECRET = "test-secret-token"


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def make_settings(**overrides: Any) -> Settings:
    """Build validated test settings. Development-safe defaults, allow-list pre-filled."""

    base: dict[str, Any] = {
        "app_env": "test",
        "log_level": "WARNING",
        "telegram_bot_token": "test-token-not-a-real-secret",
        "telegram_webhook_secret": WEBHOOK_SECRET,
        "telegram_allowed_user_ids": frozenset({USER_ID}),
        "ollama_api_key": "test-api-key-not-a-real-secret",
        # Small values so the suite is fast; the invariant that matters is enforced by
        # configuration validation (worst-case provider time <= budget - margin):
        # 2.0 x (1 + 1) + 0.25 backoff + 2.0 margin = 6.25 <= 10.
        "llm_timeout_seconds": 2.0,
        "llm_max_retries": 1,
        "request_budget_seconds": 10.0,
        "rate_limit_per_minute": 100,
    }
    base.update(overrides)
    return Settings(**base).validate_or_raise()


# --------------------------------------------------------------------------- #
# Telegram double
# --------------------------------------------------------------------------- #
@dataclass
class RecordedCall:
    method: str
    kwargs: dict[str, Any]


@dataclass
class RecordingTelegram:
    """Records every outgoing call. Counts are what the acceptance criteria assert on."""

    calls: list[RecordedCall] = field(default_factory=list)

    async def send_message(self, **kwargs: Any) -> SentMessage:
        self.calls.append(RecordedCall("sendMessage", kwargs))
        return SentMessage(message_id=len(self.calls), chat_id=int(kwargs.get("chat_id", 0)))

    async def answer_callback_query(self, **kwargs: Any) -> bool:
        self.calls.append(RecordedCall("answerCallbackQuery", kwargs))
        return True

    async def edit_message_text(self, **kwargs: Any) -> SentMessage:
        self.calls.append(RecordedCall("editMessageText", kwargs))
        return SentMessage(message_id=int(kwargs.get("message_id", 0)), chat_id=0)

    async def get_file(self, **kwargs: Any) -> DownloadedFile:
        self.calls.append(RecordedCall("getFile", kwargs))
        return DownloadedFile(file_id="f", file_path="p", file_size=0, content=b"")

    async def set_webhook(self, **kwargs: Any) -> bool:
        self.calls.append(RecordedCall("setWebhook", kwargs))
        return True

    async def delete_webhook(self, **kwargs: Any) -> bool:
        self.calls.append(RecordedCall("deleteWebhook", kwargs))
        return True

    async def get_webhook_info(self) -> dict[str, Any]:
        self.calls.append(RecordedCall("getWebhookInfo", {}))
        return {}

    async def aclose(self) -> None:  # pragma: no cover - symmetry with the real client
        return None

    # --- assertions helpers ------------------------------------------------ #
    def count_of(self, method: str) -> int:
        return sum(1 for call in self.calls if call.method == method)

    @property
    def sent_texts(self) -> list[str]:
        return [call.kwargs.get("text", "") for call in self.calls if call.method == "sendMessage"]


# --------------------------------------------------------------------------- #
# Ollama double
# --------------------------------------------------------------------------- #
def chat_response(
    *,
    content: str = "hola",
    model: str = "stub-model",
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "model": model,
        "message": message,
        "done": True,
        "total_duration": 1_500_000,
        "prompt_eval_count": 11,
        "eval_count": 7,
    }


@dataclass
class OllamaStub:
    """An ``OllamaChatClient`` wired to a ``MockTransport`` that records real requests."""

    handler: Any
    requests: list[httpx.Request] = field(default_factory=list)

    def client(
        self, *, model: str = "stub-model", max_retries: int = 0, think: str | None = None
    ) -> OllamaChatClient:
        def _handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self.handler(request)

        transport = httpx.MockTransport(_handle)
        http_client = httpx.AsyncClient(transport=transport)
        return OllamaChatClient(
            base_url="https://ollama.invalid",
            model=model,
            api_key="test-api-key-not-a-real-secret",
            timeout=2.0,
            max_retries=max_retries,
            think=think,
            client=http_client,
        )

    @property
    def chat_bodies(self) -> list[dict[str, Any]]:
        """Parsed bodies of the ``/api/chat`` requests, in order."""

        bodies = []
        for request in self.requests:
            if request.url.path.endswith("/api/chat"):
                bodies.append(json.loads(request.content.decode("utf-8")))
        return bodies

    @property
    def chat_bodies_for_model(self) -> list[str]:
        return [str(body.get("model")) for body in self.chat_bodies]


def make_db_settings(**overrides: Any) -> Settings:
    """Settings pointing at the test database."""

    base: dict[str, Any] = {
        "store_backend": "postgres",
        "database_url": TEST_DATABASE_URL,
        "database_sync_url": TEST_DATABASE_URL,
    }
    base.update(overrides)
    return make_settings(**base)


def truncate_all(settings: Settings) -> None:
    """Empty every application table (the checkpointer's included) for test isolation."""

    engine = sa.create_engine(settings.database_sync_url.get_secret_value(), poolclass=sa.pool.NullPool)
    try:
        with engine.begin() as connection:
            tables = [
                "work_items",
                "tool_invocations",
                "reminder_dispatches",
                "tasks",
                "pending_actions",
                "embedding_models",
                "document_chunks",
                "documents",
                "memories",
                "inbox_updates",
                "messages",
                "conversations",
                "checkpoint_writes",
                "checkpoint_blobs",
                "checkpoints",
            ]
            existing = {
                row[0]
                for row in connection.execute(
                    sa.text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
                )
            }
            for table in tables:
                if table in existing:
                    connection.execute(sa.text(f"TRUNCATE {table} CASCADE"))
        with engine.begin() as connection:
            # TRUNCATE removes the seeded row too, so restore the single active model.
            connection.execute(
                sa.text(
                    "INSERT INTO embedding_models (model_name, dim, pipeline_version, is_active) "
                    "VALUES ('embeddinggemma', 768, 1, true) ON CONFLICT (model_name) DO NOTHING"
                )
            )
    finally:
        engine.dispose()


@pytest.fixture
def telegram_double() -> RecordingTelegram:
    return RecordingTelegram()


@pytest.fixture
def settings() -> Settings:
    return make_settings()
