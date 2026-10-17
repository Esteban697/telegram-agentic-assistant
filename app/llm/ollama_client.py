"""Small, isolated Ollama HTTP client (PLAN.md §2.1, §10 Phase 1 deliverable 5).

Spec §3: "reutilizar el cliente existente si está en el repositorio. Si no existe,
implementar un cliente pequeño y aislado para la API de Ollama. No mezclar llamadas HTTP al
proveedor dentro de los nodos de negocio." There was no existing client, so this is the small
isolated one, and it is the only module in the project that talks to the chat endpoint.

Verified request/response shapes (PLAN.md Appendix A.1):

* ``POST {base}/api/chat`` with ``{model, messages, stream: false, tools?, format?, think?,
  options?}``; the answer is at ``message.content`` and tool calls at
  ``message.tool_calls[].function.{name,arguments}``.
* ``GET {base}/api/tags`` lists the models the endpoint advertises.
* Auth is ``Authorization: Bearer $OLLAMA_API_KEY``.
* ``stream`` defaults to **true** server-side, so it is always sent explicitly as ``false``.

Never logged here: the API key, the prompt, the completion. Logged instead: the model name,
the latency, the status and an error code (§14).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from app.errors import (
    OllamaBadResponse,
    OllamaRateLimited,
    OllamaTimeout,
    OllamaUnavailable,
    OllamaUnsupportedModel,
)
from app.llm.protocols import ChatMessage, ChatResult, ToolCall, ToolSpec
from app.policy.limits import backoff_seconds

_logger = logging.getLogger(__name__)

# Statuses worth retrying: the request never produced a side effect, so a retry is safe.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class OllamaChatClient:
    """Async client for the Ollama chat API."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: float = 30.0,
        max_retries: int = 2,
        think: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self._timeout = timeout
        self._max_retries = max(0, max_retries)
        self._think = think
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout))

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> OllamaChatClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    async def list_models(self) -> list[str]:
        """Return the advertised model names.

        Used by ``scripts/verify_ollama.py`` to print the *authoritative* model name rather
        than trusting the spelling in configuration (PLAN.md Appendix A.1, Risks item 3).
        """

        response = await self._request("GET", "/api/tags")
        try:
            payload = response.json()
            models = payload["models"]
            return [str(entry["name"]) for entry in models]
        except (KeyError, TypeError, ValueError) as exc:
            raise OllamaBadResponse("GET /api/tags returned an unexpected shape") from exc

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        response_format: str | dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> ChatResult:
        """Send one non-streaming chat request."""

        body: dict[str, Any] = {
            "model": self.model,
            "messages": [message.as_dict() for message in messages],
            # Server-side default is true; the plan pins this explicitly.
            "stream": False,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ]
        if response_format is not None:
            body["format"] = response_format
        if self._think is not None:
            body["think"] = self._think

        started = time.perf_counter()
        response = await self._request("POST", "/api/chat", json_body=body, timeout=timeout)
        latency_ms = (time.perf_counter() - started) * 1000.0

        return self._parse_chat_response(response, latency_ms=latency_ms)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        effective_timeout = self._timeout if timeout is None else timeout
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        # Absolute URL, built from our own base_url rather than from the httpx client's:
        # an injected client is a documented seam (tests, and a future shared pool), and
        # relying on its base_url would make that seam silently order-dependent.
        url = f"{self.base_url}{path}"

        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._client.request(
                    method,
                    url,
                    json=json_body,
                    headers=headers,
                    timeout=effective_timeout,
                )
            except httpx.TimeoutException as exc:
                if attempt <= self._max_retries:
                    await self._backoff(attempt)
                    continue
                raise OllamaTimeout(f"{method} {path} timed out") from exc
            except httpx.TransportError as exc:
                if attempt <= self._max_retries:
                    await self._backoff(attempt)
                    continue
                raise OllamaUnavailable(f"{method} {path} transport failure") from exc

            if response.status_code in _RETRYABLE_STATUS and attempt <= self._max_retries:
                _logger.warning(
                    "ollama retryable status",
                    extra={
                        "status": response.status_code,
                        "attempt": attempt,
                        "path": path,
                        "model": self.model,
                    },
                )
                await self._backoff(attempt)
                continue

            return self._check_status(response, method=method, path=path)

    @staticmethod
    async def _backoff(attempt: int) -> None:
        # Bounded in-process backoff inside one request; not a scheduling mechanism. The
        # schedule is shared with configuration validation via app.policy.limits, so the
        # budget arithmetic and the code that actually sleeps cannot drift apart.
        await asyncio.sleep(backoff_seconds(attempt))

    def _check_status(self, response: httpx.Response, *, method: str, path: str) -> httpx.Response:
        status = response.status_code
        if status == 429:
            raise OllamaRateLimited(f"{method} {path} rate limited")
        if status == 404:
            raise OllamaUnsupportedModel(
                f"{method} {path} returned 404 (model {self.model!r} not found?)"
            )
        if status >= 400:
            detail = _safe_error_detail(response)
            raise OllamaBadResponse(f"{method} {path} returned {status}: {detail}")
        return response

    def _parse_chat_response(self, response: httpx.Response, *, latency_ms: float) -> ChatResult:
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise OllamaBadResponse("chat response was not valid JSON") from exc

        if not isinstance(payload, dict):
            raise OllamaBadResponse("chat response was not a JSON object")

        message = payload.get("message")
        if not isinstance(message, dict):
            raise OllamaBadResponse("chat response had no 'message' object")

        content = message.get("content")
        if not isinstance(content, str):
            content = ""

        tool_calls = self._parse_tool_calls(message.get("tool_calls"))

        return ChatResult(
            content=content,
            model=str(payload.get("model") or self.model),
            tool_calls=tool_calls,
            latency_ms=latency_ms,
            duration_ms=_nanos_to_ms(payload.get("total_duration")),
            prompt_tokens=_int_or_none(payload.get("prompt_eval_count")),
            completion_tokens=_int_or_none(payload.get("eval_count")),
            raw=payload,
        )

    @staticmethod
    def _parse_tool_calls(raw_calls: Any) -> tuple[ToolCall, ...]:
        if not raw_calls:
            return ()
        if not isinstance(raw_calls, list):
            raise OllamaBadResponse("'tool_calls' was present but not a list")

        parsed: list[ToolCall] = []
        for entry in raw_calls:
            if not isinstance(entry, dict):
                raise OllamaBadResponse("a tool call entry was not an object")
            function = entry.get("function")
            if not isinstance(function, dict):
                raise OllamaBadResponse("a tool call had no 'function' object")
            name = function.get("name")
            if not isinstance(name, str) or not name:
                raise OllamaBadResponse("a tool call had no function name")
            arguments = function.get("arguments")
            if arguments is None:
                arguments = {}
            if isinstance(arguments, str):
                # Tolerated: some deployments serialise the arguments as a JSON string.
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as exc:
                    raise OllamaBadResponse(
                        f"tool call {name!r} returned arguments that were not valid JSON"
                    ) from exc
            if not isinstance(arguments, dict):
                raise OllamaBadResponse(f"tool call {name!r} arguments were not an object")
            parsed.append(
                ToolCall(
                    name=name,
                    arguments=arguments,
                    call_id=_str_or_none(entry.get("id")) or _str_or_none(function.get("index")),
                )
            )
        return tuple(parsed)


def _safe_error_detail(response: httpx.Response) -> str:
    """Return a short, structural summary of a provider error — never its body.

    **Changed in the Phase 1 review.** This used to return the provider's raw response text,
    which reached the log as ``error_detail``. A provider error envelope is exactly where a
    request echo appears: a 400 for a malformed payload comes back quoting the payload, so a
    user's message ended up in our logs (PLAN.md §7.4 forbids logging message content). The
    body is untrusted third-party text with no bounded structure, so it is not summarised —
    only its size is reported, which is enough to distinguish "provider refused the request"
    from "provider returned an HTML error page" without reproducing anything.
    """

    try:
        text = response.text
    except Exception:  # pragma: no cover - defensive
        return "body=<unreadable>"
    return f"status={response.status_code} body_bytes={len(text)}"


def _nanos_to_ms(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value) / 1_000_000.0
    return None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


__all__ = ["OllamaChatClient"]
