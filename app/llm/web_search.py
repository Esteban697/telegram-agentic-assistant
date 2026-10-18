"""Ollama Cloud web search provider (spec §10, PLAN.md §13.2).

The chat API and the search API share the same account key: ``POST /api/web_search`` with
``{"query", "max_results"}`` and ``Authorization: Bearer $OLLAMA_API_KEY`` returns
``{"results": [{"title", "url", "content"}]}`` (Ollama docs, web search capability).

This module is the only place that talks to the search endpoint. It is deliberately small and
does **not** retry: the tool's own timeout bounds the call, and a hidden retry loop would spend
the turn's budget twice. A failed search is reported as a typed error; the model decides whether
to retry the tool or tell the user.

Never logged here: the query text, the result text, or the API key. Logged instead: query length,
result count, latency and status — the same structural discipline as the chat client.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.errors import (
    OllamaBadResponse,
    OllamaRateLimited,
    OllamaTimeout,
    OllamaUnavailable,
)

_logger = logging.getLogger(__name__)

_MAX_TITLE_CHARS = 300
_MAX_URL_CHARS = 2048


class OllamaWebSearchProvider:
    """Async client for Ollama Cloud's web search endpoint."""

    def __init__(
        self,
        *,
        base_url: str = "https://ollama.com",
        api_key: str = "",
        timeout: float = 10.0,
        max_results: int = 5,
        max_content_chars: int = 2000,
        max_total_chars: int = 8000,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._max_results = max_results
        self._max_content_chars = max_content_chars
        self._max_total_chars = max_total_chars
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> OllamaWebSearchProvider:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def search(self, query: str) -> list[dict[str, str]]:
        """Return normalized results: title, URL and a bounded excerpt per result."""

        if not self._api_key:
            raise OllamaUnavailable(
                "no OLLAMA_API_KEY is configured, so web search cannot authenticate"
            )

        body: dict[str, Any] = {"query": query, "max_results": self._max_results}
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

        started = time.perf_counter()
        try:
            response = await self._client.post(
                f"{self.base_url}/api/web_search",
                json=body,
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            _logger.warning(
                "web search timed out",
                extra={"error_code": "ollama_timeout", "query_chars": len(query)},
            )
            raise OllamaTimeout("POST /api/web_search timed out") from exc
        except httpx.TransportError as exc:
            _logger.warning(
                "web search transport failure",
                extra={"error_code": "ollama_unavailable", "query_chars": len(query)},
            )
            raise OllamaUnavailable("POST /api/web_search transport failure") from exc

        latency_ms = (time.perf_counter() - started) * 1000.0
        status = response.status_code

        if status == 429:
            _logger.warning(
                "web search rate limited",
                extra={
                    "error_code": "ollama_rate_limited",
                    "status": status,
                    "duration_ms": latency_ms,
                    "query_chars": len(query),
                },
            )
            raise OllamaRateLimited("POST /api/web_search rate limited")
        if status in {401, 403}:
            # The provider's body is not echoed: it can contain account or request details.
            _logger.warning(
                "web search authentication failed",
                extra={
                    "error_code": "ollama_unavailable",
                    "status": status,
                    "duration_ms": latency_ms,
                    "query_chars": len(query),
                },
            )
            raise OllamaUnavailable(
                "POST /api/web_search rejected the API key (check OLLAMA_API_KEY)"
            )
        if not 200 <= status < 300:
            _logger.warning(
                "web search returned an error status",
                extra={
                    "error_code": "ollama_bad_response",
                    "status": status,
                    "duration_ms": latency_ms,
                    "query_chars": len(query),
                },
            )
            raise OllamaBadResponse(f"POST /api/web_search returned status {status}")

        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            raise OllamaBadResponse("web search response was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise OllamaBadResponse("web search response was not a JSON object")

        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise OllamaBadResponse("web search response had no 'results' array")

        normalized = self._normalize(raw_results)
        _logger.info(
            "web search completed",
            extra={
                "result_count": len(normalized),
                "duration_ms": latency_ms,
                "query_chars": len(query),
                "status": "ok",
            },
        )
        return normalized

    def _normalize(self, raw_results: list[Any]) -> list[dict[str, str]]:
        """Keep well-formed, http(s) results and bound what the model receives."""

        normalized: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        total_chars = 0

        for entry in raw_results[: self._max_results]:
            if not isinstance(entry, dict):
                continue
            title = entry.get("title")
            url = entry.get("url")
            content = entry.get("content")
            if not isinstance(title, str) or not isinstance(url, str):
                continue
            if not isinstance(content, str):
                content = ""

            url = url.strip()[:_MAX_URL_CHARS]
            if not url.startswith(("http://", "https://")):
                continue
            if url in seen_urls:
                continue
            remaining = self._max_total_chars - total_chars
            if remaining < 100:
                break

            excerpt = content.strip()[: min(self._max_content_chars, remaining)]
            clean_title = title.strip()[:_MAX_TITLE_CHARS] or url
            normalized.append({"title": clean_title, "url": url, "content": excerpt})
            seen_urls.add(url)
            total_chars += len(excerpt)

        return normalized


__all__ = ["OllamaWebSearchProvider"]
