"""Ollama Cloud web search: request shape, bounding, errors and tool mapping.

No network: every test uses ``httpx.MockTransport``. The provider is the only module that talks
to ``/api/web_search``; these tests pin what it sends, what it keeps from the response, and how a
provider failure becomes a typed ``ToolResult`` for the model.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from app.errors import (
    OllamaBadResponse,
    OllamaRateLimited,
    OllamaTimeout,
    OllamaUnavailable,
)
from app.llm.web_search import OllamaWebSearchProvider
from app.tools.web_search import SearchWebArgs, WebSearchTool

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
async def make_provider() -> Any:
    """Build providers sharing MockTransport clients that are closed at test end."""

    clients: list[httpx.AsyncClient] = []

    def factory(handler: Handler, **overrides: Any) -> OllamaWebSearchProvider:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        kwargs: dict[str, Any] = {
            "base_url": "https://ollama.invalid",
            "api_key": "test-api-key",
            "timeout": 1.0,
            "client": client,
        }
        kwargs.update(overrides)
        return OllamaWebSearchProvider(**kwargs)

    yield factory
    for client in clients:
        await client.aclose()


def _results(*entries: Any) -> httpx.Response:
    return httpx.Response(200, json={"results": list(entries)})


async def test_search_sends_bearer_and_bounded_request(make_provider: Any) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return _results(
            {"title": "Precio", "url": "https://example.com/a", "content": "un extracto"}
        )

    provider = make_provider(handler, max_results=7)
    results = await provider.search("precio del dólar")

    assert seen["url"] == "https://ollama.invalid/api/web_search"
    assert seen["auth"] == "Bearer test-api-key"
    assert seen["body"] == {"query": "precio del dólar", "max_results": 7}
    assert results == [
        {"title": "Precio", "url": "https://example.com/a", "content": "un extracto"}
    ]


async def test_results_are_deduplicated_and_bounded(make_provider: Any) -> None:
    long_content = "x" * 500

    def handler(_request: httpx.Request) -> httpx.Response:
        return _results(
            {"title": "Uno", "url": "https://example.com/a", "content": long_content},
            {"title": "Duplicado", "url": "https://example.com/a", "content": long_content},
            {"title": "Dos", "url": "https://example.com/b", "content": long_content},
        )

    provider = make_provider(handler, max_content_chars=100, max_total_chars=250)
    results = await provider.search("q")

    assert [item["url"] for item in results] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert all(len(item["content"]) <= 100 for item in results)
    assert sum(len(item["content"]) for item in results) <= 250


async def test_malformed_and_non_http_results_are_skipped(make_provider: Any) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _results(
            "not-an-object",
            {"title": "sin url"},
            {"title": "javascript", "url": "javascript:alert(1)", "content": "x"},
            {"title": "ok", "url": "https://ok.example", "content": "fine"},
        )

    provider = make_provider(handler)
    results = await provider.search("q")

    assert results == [{"title": "ok", "url": "https://ok.example", "content": "fine"}]


async def test_rate_limited_is_typed(make_provider: Any) -> None:
    provider = make_provider(lambda _request: httpx.Response(429))
    with pytest.raises(OllamaRateLimited):
        await provider.search("q")


async def test_authentication_failure_is_typed_without_echoing_the_body(
    make_provider: Any,
) -> None:
    provider = make_provider(
        lambda _request: httpx.Response(401, text="invalid key: super-secret")
    )
    with pytest.raises(OllamaUnavailable) as caught:
        await provider.search("q")
    assert "super-secret" not in str(caught.value)


async def test_timeout_is_typed(make_provider: Any) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    provider = make_provider(handler)
    with pytest.raises(OllamaTimeout):
        await provider.search("q")


async def test_server_error_is_typed(make_provider: Any) -> None:
    provider = make_provider(lambda _request: httpx.Response(503))
    with pytest.raises(OllamaBadResponse):
        await provider.search("q")


async def test_invalid_json_and_missing_results_are_typed(make_provider: Any) -> None:
    invalid = make_provider(lambda _request: httpx.Response(200, text="not json"))
    with pytest.raises(OllamaBadResponse):
        await invalid.search("q")

    missing = make_provider(lambda _request: httpx.Response(200, json={"other": []}))
    with pytest.raises(OllamaBadResponse):
        await missing.search("q")


async def test_missing_api_key_is_typed_unavailable(make_provider: Any) -> None:
    provider = make_provider(lambda _request: httpx.Response(200, json={"results": []}), api_key="")
    with pytest.raises(OllamaUnavailable):
        await provider.search("q")


async def test_provider_never_logs_the_query_or_the_results(
    make_provider: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    def handler(_request: httpx.Request) -> httpx.Response:
        return _results(
            {"title": "T", "url": "https://example.com", "content": "SECRET-CONTENT-XYZ"}
        )

    provider = make_provider(handler)
    await provider.search("SECRET-QUERY-XYZ")

    assert "SECRET-QUERY-XYZ" not in caplog.text
    assert "SECRET-CONTENT-XYZ" not in caplog.text


async def test_tool_maps_a_provider_failure_to_a_typed_result() -> None:
    class FailingProvider:
        async def search(self, _query: str) -> list[dict[str, str]]:
            raise OllamaTimeout("timed out")

    result = await WebSearchTool(provider=FailingProvider()).search_web(
        user_id=1, args=SearchWebArgs(query="q")
    )

    assert result.ok is False
    assert result.error_code == "ollama_timeout"
    assert result.message_key == "error.timeout"


async def test_tool_without_a_provider_is_still_capability_unavailable() -> None:
    result = await WebSearchTool().search_web(user_id=1, args=SearchWebArgs(query="q"))

    assert result.ok is False
    assert result.error_code == "CapabilityUnavailable"
    assert result.message_key == "error.capability_unavailable"
