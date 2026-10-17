"""The Telegram Bot API client — the whole method set, listed once (PLAN.md §15).

The seven methods are ``sendMessage``, ``answerCallbackQuery``, ``editMessageText``,
``getFile``, ``setWebhook``, ``deleteWebhook`` and ``getWebhookInfo``. §15 declares this list
the single authority for the count and for the names, so nothing else in the project restates
it. Hand-rolled over ``httpx`` rather than adopting ``python-telegram-bot`` (decision 12).

Three behaviours are load-bearing and easy to get wrong:

1. **``ok: false`` with HTTP 200 is a logical failure.** The Bot API reports many errors that
   way, so reading the transport status as success would silently discard them.
2. **429 carries ``retry_after``** in ``parameters``; it is surfaced on the typed error so the
   caller can decide within its own budget.
3. **``getFile`` downloads bytes, not JSON** — from the ``/file/bot<token>/<file_path>``
   endpoint — so the response must never be JSON-decoded.

The token is never logged or embedded in an error message.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.errors import TelegramApiError, TelegramError, TelegramRateLimited

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SentMessage:
    """Minimal result of a successful ``sendMessage``."""

    message_id: int
    chat_id: int


@dataclass(frozen=True, slots=True)
class DownloadedFile:
    """A file fetched through ``getFile`` plus its Telegram metadata."""

    file_id: str
    file_path: str
    file_size: int | None
    content: bytes


class TelegramClient:
    """Async, typed client for the Bot API."""

    def __init__(
        self,
        *,
        token: str,
        base_url: str = "https://api.telegram.org",
        timeout: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not token:
            # An empty token is allowed to exist so that /healthz and the test suite can run
            # without credentials (A1, A8); calling the API with it fails loudly and typed.
            _logger.warning("telegram client constructed without a bot token")
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> TelegramClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ #
    # 1. sendMessage
    # ------------------------------------------------------------------ #
    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        disable_notification: bool = False,
    ) -> SentMessage:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_notification": disable_notification,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        result = await self._call("sendMessage", payload)
        return SentMessage(
            message_id=int(result.get("message_id", 0)),
            chat_id=int(result.get("chat", {}).get("id", chat_id)),
        )

    # ------------------------------------------------------------------ #
    # 2. answerCallbackQuery
    # ------------------------------------------------------------------ #
    async def answer_callback_query(
        self,
        *,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> bool:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text is not None:
            payload["text"] = text
            payload["show_alert"] = show_alert
        result = await self._call("answerCallbackQuery", payload)
        return bool(result)

    # ------------------------------------------------------------------ #
    # 3. editMessageText
    # ------------------------------------------------------------------ #
    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> SentMessage:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = await self._call("editMessageText", payload)
        return SentMessage(
            message_id=int(result.get("message_id", message_id)),
            chat_id=int(result.get("chat", {}).get("id", chat_id)),
        )

    # ------------------------------------------------------------------ #
    # 4. getFile (+ the binary download it implies)
    # ------------------------------------------------------------------ #
    async def get_file(self, *, file_id: str, max_bytes: int | None = None) -> DownloadedFile:
        """Resolve a file id and download its bytes.

        ``max_bytes`` guards memory: the file is streamed and the download is abandoned once
        the cap is exceeded, so an oversized upload cannot exhaust the instance.
        """

        result = await self._call("getFile", {"file_id": file_id})
        file_path = str(result.get("file_path", ""))
        if not file_path:
            raise TelegramApiError("getFile returned no file_path")
        declared_size = result.get("file_size")
        size = int(declared_size) if isinstance(declared_size, int) else None

        if max_bytes is not None and size is not None and size > max_bytes:
            raise TelegramApiError(
                f"file is {size} bytes, above the {max_bytes}-byte cap",
                context={"file_size": size, "max_bytes": max_bytes},
            )

        url = f"{self._base_url}/file/bot{self._token}/{file_path}"
        chunks: list[bytes] = []
        total = 0
        try:
            async with self._client.stream("GET", url, timeout=self._timeout) as response:
                if response.status_code >= 400:
                    raise TelegramApiError(
                        f"file download returned {response.status_code}",
                        context={"status": response.status_code},
                    )
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if max_bytes is not None and total > max_bytes:
                        raise TelegramApiError(
                            "file download exceeded the configured cap",
                            context={"max_bytes": max_bytes},
                        )
                    chunks.append(chunk)
        except httpx.TimeoutException as exc:
            raise TelegramError("file download timed out") from exc
        except httpx.TransportError as exc:
            raise TelegramError("file download transport failure") from exc

        return DownloadedFile(
            file_id=file_id,
            file_path=file_path,
            file_size=size if size is not None else total,
            content=b"".join(chunks),
        )

    # ------------------------------------------------------------------ #
    # 5-7. Webhook management
    # ------------------------------------------------------------------ #
    async def set_webhook(
        self,
        *,
        url: str,
        secret_token: str,
        allowed_updates: list[str] | None = None,
        drop_pending_updates: bool = False,
    ) -> bool:
        payload: dict[str, Any] = {
            "url": url,
            "secret_token": secret_token,
            "drop_pending_updates": drop_pending_updates,
        }
        if allowed_updates is not None:
            payload["allowed_updates"] = allowed_updates
        result = await self._call("setWebhook", payload)
        return bool(result)

    async def delete_webhook(self, *, drop_pending_updates: bool = False) -> bool:
        result = await self._call(
            "deleteWebhook", {"drop_pending_updates": drop_pending_updates}
        )
        return bool(result)

    async def get_webhook_info(self) -> dict[str, Any]:
        result = await self._call("getWebhookInfo", {})
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    async def _call(self, method: str, payload: dict[str, Any]) -> Any:
        url = f"{self._base_url}/bot{self._token}/{method}"
        try:
            response = await self._client.post(url, json=payload, timeout=self._timeout)
        except httpx.TimeoutException as exc:
            raise TelegramError(f"{method} timed out") from exc
        except httpx.TransportError as exc:
            raise TelegramError(f"{method} transport failure") from exc

        if response.status_code == 429:
            retry_after = _retry_after_of(response)
            _logger.warning(
                "telegram rate limited",
                extra={"error_code": "telegram_rate_limited", "retry_after": retry_after},
            )
            raise TelegramRateLimited(f"{method} returned 429", retry_after=retry_after)

        try:
            body = response.json()
        except ValueError as exc:
            raise TelegramApiError(f"{method} returned a non-JSON body") from exc

        if not isinstance(body, dict):
            raise TelegramApiError(f"{method} returned a non-object body")

        if not body.get("ok", False):
            # HTTP 200 with ok:false is a logical failure — this is the case the plan calls out.
            description = str(body.get("description", "unknown error"))
            error_code = body.get("error_code")
            if error_code == 429:
                raise TelegramRateLimited(
                    f"{method} reported 429: {description}",
                    retry_after=_retry_after_from_body(body),
                )
            raise TelegramApiError(
                f"{method} failed: {description}",
                context={"telegram_error_code": error_code, "status": response.status_code},
            )

        return body.get("result")


def _retry_after_of(response: httpx.Response) -> int | None:
    try:
        return _retry_after_from_body(response.json())
    except ValueError:
        return None


def _retry_after_from_body(body: Any) -> int | None:
    if not isinstance(body, dict):
        return None
    parameters = body.get("parameters")
    if isinstance(parameters, dict):
        value = parameters.get("retry_after")
        if isinstance(value, int):
            return value
    return None


__all__ = ["DownloadedFile", "SentMessage", "TelegramClient"]
