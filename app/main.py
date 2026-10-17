"""Application factory and dependency wiring (PLAN.md §10 Phase 1 deliverable 11).

This is the **only** place concrete implementations are chosen: settings are read, the database
and checkpointer are opened, the stores, tools and services are built, and the agent is compiled
against whatever checkpointer the deployment provides.

Startup is fail-fast (PLAN.md §8.1): an invalid configuration, or one §7.2 forbids in
production, raises before the process can serve a request (A13).

Phase 2 wiring note: conversational history now comes from the **checkpointed thread state**
(NN1), so `ChatAgent` is the `HistoryProvider` the conversation store reads. `agent/` still never
imports `app.storage` — the protocol is declared in the storage layer and satisfied structurally.
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
from app.services.conversation import ConversationService
from app.storage.checkpointing import build_checkpointer, setup_checkpointer
from app.storage.postgres import Database, build_database, use_compatible_event_loop
from app.storage.stores import build_stores
from app.tools.memory import MemoryTools
from app.tools.tasks import TaskTools

_logger = logging.getLogger(__name__)

# Windows only: psycopg's async mode cannot run on the default ProactorEventLoop, so the
# policy is installed before uvicorn (or any caller) creates the loop. No-op elsewhere.
use_compatible_event_loop()


def create_app(
    settings: Settings | None = None,
    *,
    chat_model: ChatModel | None = None,
    telegram_client: TelegramClient | None = None,
    database: Database | None = None,
    checkpointer: Any | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    The optional arguments exist so the credential-free test suites can inject a fake model, a
    mock transport and a test database; production passes none of them and the real adapters
    are built from configuration.
    """

    resolved = (settings or load_settings()).validate_or_raise()
    configure_logging(
        level=resolved.log_level,
        app_env=resolved.app_env,
        content_debug=resolved.log_content_debug,
    )

    for warning in resolved.access_warnings():
        _logger.warning(warning, extra={"error_code": "insecure_development_configuration"})

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owns_database = database is None and resolved.store_backend == "postgres"
        owns_checkpointer = False

        active_database = database
        saver = None
        if resolved.store_backend == "postgres":
            if active_database is None:
                active_database = build_database(resolved)
            if checkpointer is not None:
                # Either a built Checkpointer (with its pool) or a raw saver. `setup()` belongs
                # to whichever we were given: calling `setup_checkpointer` with a bare saver
                # raised `AttributeError: 'State' object has no attribute 'checkpointer'`.
                built_checkpointer = getattr(checkpointer, "saver", None) is not None
                saver = checkpointer.saver if built_checkpointer else checkpointer
                if built_checkpointer and resolved.migrations_autorun:
                    await setup_checkpointer(checkpointer)
                elif resolved.migrations_autorun:
                    await saver.setup()
            else:
                built = await build_checkpointer(resolved)
                saver = built.saver
                owns_checkpointer = True
                app.state.checkpointer = built
                if resolved.migrations_autorun:
                    # Off by default and refused in production (PLAN.md §11.6): a schema change
                    # must never be a side effect of starting a service.
                    await setup_checkpointer(built)

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
            checkpointer=saver,
        )

        dedupe, conversations = build_stores(
            store_backend=resolved.store_backend,
            app_env=resolved.app_env,
            update_dedupe_ttl_seconds=resolved.update_dedupe_ttl_seconds,
            update_dedupe_max_entries=resolved.update_dedupe_max_entries,
            context_max_messages=resolved.context_max_messages,
            context_ttl_seconds=resolved.context_ttl_seconds,
            session_factory=None if active_database is None else active_database.sessions,
            history_provider=agent,
        )
        conversation_service = ConversationService(
            conversations, max_messages=resolved.context_max_messages
        )

        memory_tools = (
            None if active_database is None else MemoryTools(sessions=active_database.sessions)
        )
        task_tools = (
            None
            if active_database is None
            else TaskTools(
                sessions=active_database.sessions,
                max_attempts=resolved.reminder_max_attempts,
                default_timezone=resolved.default_timezone,
            )
        )

        app.state.settings = resolved
        app.state.database = active_database
        app.state.dedupe = dedupe
        app.state.conversations = conversations
        app.state.conversation_service = conversation_service
        app.state.chat_model = model
        app.state.telegram = telegram
        app.state.agent = agent
        app.state.memory_tools = memory_tools
        app.state.task_tools = task_tools
        app.state.rate_limiter = RateLimiter(per_minute=resolved.rate_limit_per_minute)
        app.state.handlers = TelegramHandlers(
            settings=resolved,
            telegram=telegram,
            agent=agent,
            conversations=conversation_service,
            memory_tools=memory_tools,
            task_tools=task_tools,
        )

        _logger.info(
            "application started",
            extra={
                "model": resolved.llm_model,
                "store_backend": resolved.store_backend,
                "durable_conversations": conversations is not None,
                "status": "ready",
            },
        )
        try:
            yield
        finally:
            if chat_model is None and isinstance(model, OllamaChatClient):
                await model.aclose()
            if telegram_client is None:
                await telegram.aclose()
            if owns_checkpointer and getattr(app.state, "checkpointer", None) is not None:
                await app.state.checkpointer.aclose()
            if owns_database and active_database is not None:
                await active_database.dispose()
            _logger.info("application stopped", extra={"status": "stopped"})

    app = FastAPI(
        title="Telegram personal assistant",
        version="0.2.0",
        lifespan=lifespan,
        # The schema is off: this service exposes an unauthenticated webhook and health
        # endpoints, and there is nothing for a public API document to help with.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(health_router)
    app.include_router(webhook_router)
    app.state.settings = resolved
    return app


def main() -> Any:  # pragma: no cover - entry point for `python -m app.main`
    """Run the service with uvicorn. Used by the README's local flow."""

    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, log_config=None)


# Module-level app for `uvicorn app.main:app`. Settings are read (and validated) at import,
# so a misconfigured deployment fails immediately rather than on first request (A13).
app = create_app()


__all__ = ["app", "create_app", "main"]


if __name__ == "__main__":  # pragma: no cover
    main()
