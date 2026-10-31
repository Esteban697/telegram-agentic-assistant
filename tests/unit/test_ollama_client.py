"""The Ollama client: request shape, response parsing, typed error mapping (§10 deliverable 5)."""

from __future__ import annotations

import base64

import httpx
import pytest

from app.errors import (
    OllamaBadResponse,
    OllamaRateLimited,
    OllamaTimeout,
    OllamaUnavailable,
    OllamaUnsupportedModel,
)
from app.llm.ollama_client import OllamaChatClient
from app.llm.protocols import ChatMessage, ToolSpec
from tests.conftest import OllamaStub, chat_response


def _client(handler, **kwargs) -> OllamaChatClient:
    stub = OllamaStub(handler=handler)
    return stub.client(**kwargs), stub


# --------------------------------------------------------------------------- #
# Request shape
# --------------------------------------------------------------------------- #
async def test_request_body_matches_the_documented_shape() -> None:
    """Appendix A.1: model + messages, and ``stream`` explicitly false."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=chat_response(content="hola"))

    client, stub = _client(handler)
    await client.chat([ChatMessage(role="user", content="hola")])

    request = stub.requests[0]
    assert request.url.path == "/api/chat"
    assert request.headers["authorization"].startswith("Bearer ")
    body = stub.chat_bodies[0]
    assert body["model"] == "stub-model"
    assert body["stream"] is False
    assert body["messages"] == [{"role": "user", "content": "hola"}]
    assert "tools" not in body


async def test_tools_are_wrapped_in_the_function_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=chat_response(content="ok"))

    tool = ToolSpec(
        name="get_current_weather",
        description="Weather lookup",
        parameters={"type": "object", "properties": {"location": {"type": "string"}}},
    )
    client, stub = _client(handler)
    await client.chat([ChatMessage(role="user", content="tiempo en Madrid")], tools=[tool])

    body = stub.chat_bodies[0]
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_current_weather",
                "description": "Weather lookup",
                "parameters": tool.parameters,
            },
        }
    ]


async def test_response_format_is_forwarded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=chat_response(content='{"answer":"Madrid"}'))

    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    client, stub = _client(handler)
    await client.chat([ChatMessage(role="user", content="capital")], response_format=schema)
    assert stub.chat_bodies[0]["format"] == schema


async def test_think_is_only_sent_when_configured() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=chat_response())

    stub = OllamaStub(handler=handler)
    await stub.client().chat([ChatMessage(role="user", content="x")])
    assert "think" not in stub.chat_bodies[0]

    await stub.client(think="low").chat([ChatMessage(role="user", content="x")])
    assert stub.chat_bodies[1]["think"] == "low"


# --------------------------------------------------------------------------- #
# Response parsing
# --------------------------------------------------------------------------- #
async def test_parses_content_and_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=chat_response(content="buenas", model="real-model"))

    client, _ = _client(handler)
    result = await client.chat([ChatMessage(role="user", content="hola")])

    assert result.content == "buenas"
    assert result.model == "real-model"
    assert result.prompt_tokens == 11
    assert result.completion_tokens == 7
    assert result.latency_ms is not None and result.latency_ms >= 0
    assert result.duration_ms == pytest.approx(1.5)
    assert not result.has_tool_calls


async def test_parses_tool_calls_with_object_arguments() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=chat_response(
                content="",
                tool_calls=[
                    {
                        "function": {
                            "name": "get_current_weather",
                            "arguments": {"location": "Madrid", "unit": "celsius"},
                        }
                    }
                ],
            ),
        )

    client, _ = _client(handler)
    result = await client.chat([ChatMessage(role="user", content="tiempo")])

    assert result.has_tool_calls
    call = result.tool_calls[0]
    assert call.name == "get_current_weather"
    assert call.arguments == {"location": "Madrid", "unit": "celsius"}


async def test_tolerates_arguments_serialised_as_a_json_string() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=chat_response(
                content="",
                tool_calls=[
                    {"function": {"name": "t", "arguments": '{"a": 1}'}}
                ],
            ),
        )

    client, _ = _client(handler)
    result = await client.chat([ChatMessage(role="user", content="x")])
    assert result.tool_calls[0].arguments == {"a": 1}


@pytest.mark.parametrize(
    "payload",
    [
        {"message": {"content": ""}},  # no tool_calls key -> fine, no calls
        {"model": "m"},  # no message -> bad response
        {"message": {"tool_calls": [{"function": {"name": "t", "arguments": "{not json"}}]}},
    ],
)
async def test_malformed_responses_are_typed_errors(payload: dict) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client, _ = _client(handler)
    try:
        result = await client.chat([ChatMessage(role="user", content="x")])
    except OllamaBadResponse:
        return
    assert not result.has_tool_calls


async def test_non_json_body_is_a_bad_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>nope</html>")

    client, _ = _client(handler)
    with pytest.raises(OllamaBadResponse):
        await client.chat([ChatMessage(role="user", content="x")])


# --------------------------------------------------------------------------- #
# Typed error mapping
# --------------------------------------------------------------------------- #
async def test_timeout_maps_to_ollama_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    client, _ = _client(handler, max_retries=0)
    with pytest.raises(OllamaTimeout):
        await client.chat([ChatMessage(role="user", content="x")])


async def test_transport_failure_maps_to_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    client, _ = _client(handler, max_retries=0)
    with pytest.raises(OllamaUnavailable):
        await client.chat([ChatMessage(role="user", content="x")])


async def test_rate_limit_maps_to_ollama_rate_limited() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "slow down"})

    client, _ = _client(handler, max_retries=0)
    with pytest.raises(OllamaRateLimited):
        await client.chat([ChatMessage(role="user", content="x")])


async def test_unknown_model_maps_to_unsupported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "model not found"})

    client, _ = _client(handler, max_retries=0)
    with pytest.raises(OllamaUnsupportedModel):
        await client.chat([ChatMessage(role="user", content="x")])


async def test_server_error_is_retried_then_reported() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(500, json={"error": "boom"})

    client, _ = _client(handler, max_retries=2)
    with pytest.raises(OllamaBadResponse):
        await client.chat([ChatMessage(role="user", content="x")])
    assert len(attempts) == 3, "a 5xx must be retried up to max_retries, then fail"


async def test_a_retryable_status_recovers_without_reporting_an_error() -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        if len(seen) == 1:
            return httpx.Response(503, json={"error": "warming up"})
        return httpx.Response(200, json=chat_response(content="ya"))

    client, _ = _client(handler, max_retries=2)
    result = await client.chat([ChatMessage(role="user", content="x")])
    assert result.content == "ya"
    assert len(seen) == 2


async def test_images_are_base64_encoded_in_the_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=chat_response(content="rojo"))

    client, stub = _client(handler)
    image = b"\x89PNG\r\n\x1a\nfake-image-bytes"
    await client.chat(
        [ChatMessage(role="user", content="¿De qué color es?", images=(image,))]
    )

    body = stub.chat_bodies[0]
    assert body["messages"][0]["images"] == [base64.b64encode(image).decode("ascii")]


# --------------------------------------------------------------------------- #
# Model catalogue
# --------------------------------------------------------------------------- #
async def test_list_models_returns_advertised_names() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": "gpt-oss:120b"}, {"name": "x:y"}]})

    client, _ = _client(handler)
    assert await client.list_models() == ["gpt-oss:120b", "x:y"]


async def test_list_models_rejects_an_unexpected_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"nope": []})

    client, _ = _client(handler)
    with pytest.raises(OllamaBadResponse):
        await client.list_models()
