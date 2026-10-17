"""Application factory and dependency wiring (PLAN.md §10 Phase 1 deliverable 11).

This is the **only** place concrete implementations are chosen: the settings are read, the
stores are selected, the Ollama client, the graph, the Telegram client and the handlers are
constructed and attached to ``app.state``. Everything else receives its collaborators.

Startup is fail-fast (PLAN.md §8.1): an invalid configuration, or one that §7.2 forbids in
production, raises before the process can serve a request (A13).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from app.agent.graph import ChatAgent
from app.agent.prompts import build_system_prompt
from app.api.health import router as health_router
from app.api.telegram_webhook import RateLimiter
from app.api.telegram_webhook import router as webhook_router
from app.bot.handlers import TelegramHandlers
from app.bot.telegram_client import TelegramClient
from app.config import Settings, load_settings
from app.llm.ollama_client import OllamaChatClient
from app.llm.protocols import ChatModel
from app.logging_config import configure_logging
from app.storage.stores import build_stores

_logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    chat_model: ChatModel | None = None,
    telegram_client: TelegramClient | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    ``chat_model`` and ``telegram_client`` exist so the credential-free test suites can inject
    a fake model and a mock transport; production passes neither and the real adapters are
    built from configuration.
    """

    resolved = (settings or load_settings()).validate_or_raise()
    configure_logging(
        level=resolved.log_level,
        app_env=resolved.app_env,
        content_debug=resolved.log_content_debug,
    )

    # The loud warnings of PLAN.md §7.2. Emitted before anything else can fail, so the
    # operator sees them on the first screenful rather than discovering the situation from
    # behaviour: an empty allow-list denies everybody, and DEV_ALLOW_ANY_USER admits anybody.
    for warning in resolved.access_warnings():
        _logger.warning(warning, extra={"error_code": "insecure_development_configuration"})

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        dedupe, conversations = build_stores(
            store_backend=resolved.store_backend,
            app_env=resolved.app_env,
            update_dedupe_ttl_seconds=resolved.update_dedupe_ttl_seconds,
            update_dedupe_max_entries=resolved.update_dedupe_max_entries,
            context_max_messages=resolved.context_max_messages,
            context_ttl_seconds=resolved.context_ttl_seconds,
        )

        model: ChatModel = chat_model or OllamaChatClient(
            base_url=resolved.ollama_base_url,
            model=resolved.llm_model,
            api_key=resolved.api_key,
            timeout=resolved.llm_timeout_seconds,
            max_retries=resolved.llm_max_retries,
            think=resolved.llm_think,
        )
        telegram = telegram_client or TelegramClient(
            token=resolved.bot_token,
            base_url=resolved.telegram_api_base_url,
        )
        agent = ChatAgent(
            model,
            system_prompt=build_system_prompt(),
            budget_seconds=resolved.request_budget_seconds,
        )

        app.state.settings = resolved
        app.state.dedupe = dedupe
        app.state.conversations = conversations
        app.state.chat_model = model
        app.state.telegram = telegram
        app.state.agent = agent
        app.state.rate_limiter = RateLimiter(per_minute=resolved.rate_limit_per_minute)
        app.state.handlers = TelegramHandlers(
            settings=resolved,
            telegram=telegram,
            agent=agent,
            conversations=conversations,
        )

        _logger.info(
            "application started",
            extra={
                "model": resolved.llm_model,
                "store_backend": resolved.store_backend,
                "status": "ready",
            },
        )
        try:
            yield
        finally:
            # Close only what we created: an injected client belongs to its injector.
            if chat_model is None and isinstance(model, OllamaChatClient):
                await model.aclose()
            if telegram_client is None:
                await telegram.aclose()
            _logger.info("application stopped", extra={"status": "stopped"})

    app = FastAPI(
        title="Telegram personal assistant",
        version="0.1.0",
        lifespan=lifespan,
        # The schema is off: this service exposes an unauthenticated webhook and health
        # endpoints, and there is nothing for a public API document to help with.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(health_router)
    app.include_router(webhook_router)

    # Surface the resolved settings for tools/tests without starting a server.
    app.state.settings = resolved
    return app


def main() -> Any:  # pragma: no cover - entry point for `python -m app.main`
    """Run the service with uvicorn. Used by the README's local flow."""

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        log_config=None,
    )


# Module-level app for `uvicorn app.main:app`. Settings are read (and validated) at import,
# so a misconfigured deployment fails immediately rather than on first request (A13).
app = create_app()


__all__ = ["app", "create_app", "main"]


if __name__ == "__main__":  # pragma: no cover
    main()
