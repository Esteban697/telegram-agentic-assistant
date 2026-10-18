"""Live evaluation: the agent reaches the web through Ollama Cloud.

This test is ``eval`` **and** ``live``: it calls the real model and ``POST /api/web_search``.
It is skipped without ``OLLAMA_API_KEY`` or ``TEST_DATABASE_URL`` and never runs in the default
suite. The point is the integration — the model asks for the tool, the provider bounds the
results, and the answer comes back with a figure instead of a refusal. It is deliberately
explicit about searching ("Busca en la web..."), so a failure means the wiring broke, not that a
vague question was ambiguous.
"""

from __future__ import annotations

import os
import pathlib
from typing import Any

import pytest

from app.agent.graph import ChatAgent
from app.agent.prompts import build_system_prompt
from app.config import Settings
from app.llm.ollama_client import OllamaChatClient
from app.llm.protocols import ChatMessage, ChatResult, ToolSpec
from app.llm.web_search import OllamaWebSearchProvider
from app.tools.registry import ToolRegistry
from app.tools.web_search import WebSearchTool
from app.tools.web_search import specs as web_specs
from tests.evals.conftest import RagHarness, requires_database


def _api_key() -> str:
    if os.environ.get("OLLAMA_API_KEY"):
        return os.environ["OLLAMA_API_KEY"]
    env_file = pathlib.Path(".env")
    if not env_file.exists():
        return ""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("OLLAMA_API_KEY="):
            return line.split("=", 1)[1].strip().strip("'\"")
    return ""


API_KEY = _api_key()

pytestmark = [
    pytest.mark.eval,
    pytest.mark.live,
    requires_database,
    pytest.mark.skipif(
        not API_KEY, reason="OLLAMA_API_KEY is not set: export it or put it in .env to run these"
    ),
]


class RecordingModel:
    """The real client, wrapped to record which tools the model actually requested."""

    def __init__(self, inner: OllamaChatClient) -> None:
        self.inner = inner
        self.requested: list[str] = []

    @property
    def model(self) -> str:
        return self.inner.model

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        response_format: str | dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> ChatResult:
        result = await self.inner.chat(
            messages, tools=tools, response_format=response_format, timeout=timeout
        )
        self.requested.extend(call.name for call in result.tool_calls)
        return result


class WebSearchHarness(RagHarness):
    """The real graph with a registry that contains only the real web search tool."""

    async def __aenter__(self) -> WebSearchHarness:
        await super().__aenter__()
        live = Settings()
        self.provider = OllamaWebSearchProvider(
            base_url=live.web_search_base_url,
            api_key=API_KEY,
            timeout=live.web_search_timeout_seconds,
            max_results=live.web_search_max_results,
            max_content_chars=live.web_search_max_content_chars,
            max_total_chars=live.web_search_max_total_chars,
        )
        self.registry = ToolRegistry(
            [*web_specs(WebSearchTool(provider=self.provider))],
            sessions=self.database.sessions,
            stale_seconds=live.stale_invocation_seconds,
        )
        self.model = RecordingModel(
            OllamaChatClient(
                base_url=live.ollama_base_url,
                model=live.llm_model,
                api_key=API_KEY,
                timeout=120.0,
                max_retries=1,
                think=live.llm_think,
            )
        )
        self.agent = ChatAgent(
            self.model,
            system_prompt=build_system_prompt(),
            budget_seconds=180.0,
            registry=self.registry,
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.model.inner.aclose()
        await self.provider.aclose()
        await super().__aexit__(*_exc)


@pytest.fixture
async def web_agent() -> WebSearchHarness:
    async with WebSearchHarness() as harness:
        yield harness


async def test_the_agent_searches_the_web_and_answers_with_a_current_figure(
    web_agent: WebSearchHarness,
) -> None:
    result = await web_agent.agent.run(
        [
            ChatMessage(
                role="user",
                content=(
                    "Busca en la web el precio actual del bitcoin y dime la cifra. "
                    "Cita el título y la URL de la fuente que uses."
                ),
            )
        ],
        thread_id="eval-web-search",
        user_id=42,
        chat_id=100,
    )

    assert "search_web" in web_agent.model.requested, (
        "the model did not ask for search_web; tools requested: "
        f"{web_agent.model.requested}, reply: {result.content!r}"
    )
    assert result.content.strip(), "the agent returned an empty reply"
    lowered = result.content.lower()
    for refusal in ("no puedo", "no tengo acceso", "no dispongo de"):
        assert refusal not in lowered, (
            f"the agent refused although search_web was available: {result.content!r}"
        )
    assert any(char.isdigit() for char in result.content), (
        f"the answer contains no figure despite a web search: {result.content!r}"
    )
