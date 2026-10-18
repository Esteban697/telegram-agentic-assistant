"""Application factory and dependency wiring (PLAN.md §10 Phase 1 deliverable 11, Phase 2/3).

This is the **only** place concrete implementations are chosen. The construction order is not
arbitrary:

1. settings (validated, fail-fast);
2. database and checkpointer (the durability layer);
3. the tools and the **registry** — which refuses to load a tool without a risk level — plus the
   approval service and the resume queue;
4. the model, the Telegram client and the **agent**, which needs the registry and the approvals;
5. the stores — the conversation store reads history from the agent's checkpointed state (NN1),
   which is why the agent exists first;
6. the handlers, which need everything above.

Startup is fail-fast (§8.1): an invalid configuration, or one §7.2 forbids in production, raises
before the process can serve a request.
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
from app.errors import AssistantError
from app.bot.telegram_client import TelegramClient
from app.config import Settings, load_settings
from app.llm.embeddings import build_embedding_provider
from app.llm.ollama_client import OllamaChatClient
from app.llm.protocols import ChatModel
from app.llm.web_search import OllamaWebSearchProvider
from app.logging_config import configure_logging
from app.services.approvals import ApprovalService
from app.services.conversation import ConversationService
from app.services.document_ingestion import DocumentIngestion

# Inline ingestion must leave room in the request for the reply itself.
INGEST_BUDGET_FRACTION = 0.6
from app.services.work_queue import WorkQueue
from app.storage.checkpointing import build_checkpointer, setup_checkpointer
from app.storage.gcs import build_object_store
from app.storage.postgres import Database, build_database, use_compatible_event_loop
from app.storage.stores import build_stores
from app.tools.documents import DocumentTools
from app.tools.documents import specs as document_specs
from app.tools.memory import MemoryTools
from app.tools.memory import specs as memory_specs
from app.tools.registry import ToolRegistry
from app.tools.restaurants import RestaurantTools
from app.tools.restaurants import specs as restaurant_specs
from app.tools.tasks import TaskTools
from app.tools.tasks import specs as task_specs
from app.tools.web_search import WebSearchTool
from app.tools.web_search import specs as web_specs

_logger = logging.getLogger(__name__)

# Windows only: psycopg's async mode cannot run on the default ProactorEventLoop, so the policy is
# installed before uvicorn (or any caller) creates the loop. No-op elsewhere.
use_compatible_event_loop()


def create_app(
    settings: Settings | None = None,
    *,
    chat_model: ChatModel | None = None,
    telegram_client: TelegramClient | None = None,
    database: Database | None = None,
    checkpointer: Any | None = None,
) -> FastAPI:
    """Build the FastAPI application."""

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
                # Either a built Checkpointer (with its pool) or a raw saver.
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
                    await setup_checkpointer(built)

        # --- tools, registry, approvals, queue ------------------------------------------- #
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

        # Phase 4: the embedding provider (§13.1: Vertex everywhere), the object store and the
        # ingestion pipeline. The provider is built even when a database is absent so the app
        # reports the capability problem plainly (no ADC) rather than at first use.
        # A missing embedding capability must not stop the service from booting: chat, memory
        # and reminders do not need it, and the failure should surface where it matters — when a
        # document arrives — with the exact remediation rather than as a startup crash.
        embedding_error: str | None = None
        try:
            embeddings = build_embedding_provider(resolved)
        except AssistantError as exc:
            embeddings = None
            embedding_error = exc.detail or exc.code
            _logger.warning(
                "the embedding provider is unavailable; documents are disabled",
                extra=exc.as_log_fields(),
            )
        objects = build_object_store(resolved)
        ingestion = (
            None
            if active_database is None or embeddings is None
            else DocumentIngestion(
                sessions=active_database.sessions,
                objects=objects,
                embeddings=embeddings,
                active_model_name=resolved.embed_model,
                max_upload_mb=resolved.max_upload_mb,
                max_pages=resolved.max_pdf_pages,
                max_ingest_seconds=resolved.max_ingest_seconds,
                # Spec §6.1: inline only if the budget allows. The deadline is the smaller of
                # the configured ceiling and a fraction of the request budget, so the webhook
                # always answers inside Telegram's deadline; the remainder is the Phase 5 job's.
                deadline_seconds=min(
                    resolved.ingest_inline_budget_seconds,
                    resolved.request_budget_seconds * INGEST_BUDGET_FRACTION,
                ),
                chunk_chars=resolved.rag_chunk_chars,
                chunk_overlap=resolved.rag_chunk_overlap_chars,
                chunk_max=resolved.rag_chunk_max_chars,
                chunk_min=resolved.rag_chunk_min_chars,
            )
        )
        document_tools = (
            None
            if active_database is None or embeddings is None
            else DocumentTools(
                sessions=active_database.sessions,
                embeddings=embeddings,
                max_context_chars=resolved.rag_max_context_chars,
                top_k=resolved.rag_top_k,
                min_score=resolved.rag_min_score,
                ingestion=ingestion,
            )
        )

        # --- web search ---------------------------------------------------------------- #
        # The same Ollama account key authenticates chat and search. Without a key the tool
        # still exists but reports CapabilityUnavailable, which is honest and keeps the local
        # credential-free setup working.
        web_provider = None
        if (
            resolved.web_search_provider == "ollama"
            and resolved.api_key
            and resolved.app_env != "test"
        ):
            web_provider = OllamaWebSearchProvider(
                base_url=resolved.web_search_base_url,
                api_key=resolved.api_key,
                timeout=resolved.web_search_timeout_seconds,
                max_results=resolved.web_search_max_results,
                max_content_chars=resolved.web_search_max_content_chars,
                max_total_chars=resolved.web_search_max_total_chars,
            )

        registry: ToolRegistry | None = None
        approvals: ApprovalService | None = None
        work_queue: WorkQueue | None = None
        if (
            active_database is not None
            and memory_tools is not None
            and task_tools is not None
            and document_tools is not None
        ):
            registry = ToolRegistry(
                [
                    *memory_specs(memory_tools),
                    *task_specs(task_tools),
                    *web_specs(WebSearchTool(provider=web_provider)),
                    *restaurant_specs(RestaurantTools()),
                    *document_specs(document_tools),
                ],
                sessions=active_database.sessions,
                stale_seconds=resolved.stale_invocation_seconds,
            )
            approvals = ApprovalService(
                sessions=active_database.sessions, ttl_minutes=resolved.approval_ttl_minutes
            )
            work_queue = WorkQueue(
                sessions=active_database.sessions,
                lease_seconds=resolved.dispatcher_lease_seconds,
            )

        # --- model, transport, agent ----------------------------------------------------- #
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
            registry=registry,
            approvals=approvals,
        )

        # --- stores (history comes from the agent's checkpoints, NN1) -------------------- #
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
        # /readyz reads these (§8.2/RN12): the database, the vector extension and the single
        # active embedding model row are dependencies it must report on.
        app.state.settings = resolved
        app.state.database = active_database
        app.state.embeddings = embeddings
        app.state.embedding_error = embedding_error
        app.state.objects = objects
        app.state.ingestion = ingestion
        app.state.document_tools = document_tools
        app.state.registry = registry
        app.state.approvals = approvals
        app.state.work_queue = work_queue
        app.state.rate_limiter = RateLimiter(per_minute=resolved.rate_limit_per_minute)
        app.state.handlers = TelegramHandlers(
            settings=resolved,
            telegram=telegram,
            agent=agent,
            conversations=conversation_service,
            memory_tools=memory_tools,
            task_tools=task_tools,
            approvals=approvals,
            work_queue=work_queue,
            registry=registry,
            ingestion=ingestion,
            document_tools=document_tools,
        )

        _logger.info(
            "application started",
            extra={
                "model": resolved.llm_model,
                "store_backend": resolved.store_backend,
                "tools": len(registry.names()) if registry else 0,
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
            if web_provider is not None:
                await web_provider.aclose()
            if owns_checkpointer and getattr(app.state, "checkpointer", None) is not None:
                await app.state.checkpointer.aclose()
            if owns_database and active_database is not None:
                await active_database.dispose()
            _logger.info("application stopped", extra={"status": "stopped"})

    app = FastAPI(
        title="Telegram personal assistant",
        version="0.3.0",
        lifespan=lifespan,
        # The schema is off: this service exposes an unauthenticated webhook and health endpoints.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(health_router)
    app.include_router(webhook_router)
    app.state.settings = resolved
    return app


def main() -> Any:  # pragma: no cover - entry point for `python -m app.main`
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, log_config=None)


# Module-level app for `uvicorn app.main:app`.
app = create_app()


__all__ = ["app", "create_app", "main"]


if __name__ == "__main__":  # pragma: no cover
    main()
