"""The Telegram client: the ``ok:false`` envelope, 429, and the multipart download (§10
Phase 1 deliverable 6)."""

from __future__ import annotations

import httpx
import pytest

from app.bot.telegram_client import TelegramClient
from app.errors import TelegramApiError, TelegramError, TelegramRateLimited

TOKEN = "123:test-token"


def _client(handler) -> tuple[TelegramClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = TelegramClient(
        token=TOKEN,
        base_url="https://telegram.invalid",
        client=httpx.AsyncClient(transport=httpx.MockTransport(_handle)),
    )
    return client, requests


# --------------------------------------------------------------------------- #
# sendMessage
# --------------------------------------------------------------------------- #
async def test_send_message_posts_the_expected_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/bot{TOKEN}/sendMessage"
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 5, "chat": {"id": 4242}}}
        )

    client, requests = _client(handler)
    sent = await client.send_message(chat_id=4242, text="hola", reply_markup={"inline_keyboard": []})

    assert sent.message_id == 5
    assert sent.chat_id == 4242
    body = requests[0].content.decode()
    assert '"chat_id"' in body and '"reply_markup"' in body


async def test_http_200_with_ok_false_is_a_logical_failure() -> None:
    """The case the plan calls out: the transport succeeded, the request did not."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": False,
                "error_code": 400,
                "description": "Bad Request: chat not found",
            },
        )

    client, _ = _client(handler)
    with pytest.raises(TelegramApiError) as excinfo:
        await client.send_message(chat_id=1, text="x")
    assert "chat not found" in str(excinfo.value)
    assert excinfo.value.context["telegram_error_code"] == 400


async def test_429_carries_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 17},
            },
        )

    client, _ = _client(handler)
    with pytest.raises(TelegramRateLimited) as excinfo:
        await client.send_message(chat_id=1, text="x")
    assert excinfo.value.retry_after == 17


async def test_429_reported_inside_a_200_envelope_also_carries_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 3},
            },
        )

    client, _ = _client(handler)
    with pytest.raises(TelegramRateLimited) as excinfo:
        await client.send_message(chat_id=1, text="x")
    assert excinfo.value.retry_after == 3


async def test_non_json_body_is_reported_as_an_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>bad gateway</html>")

    client, _ = _client(handler)
    with pytest.raises(TelegramApiError):
        await client.send_message(chat_id=1, text="x")


async def test_transport_failure_is_reported_as_a_telegram_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    client, _ = _client(handler)
    with pytest.raises(TelegramError):
        await client.send_message(chat_id=1, text="x")


# --------------------------------------------------------------------------- #
# answerCallbackQuery / editMessageText
# --------------------------------------------------------------------------- #
async def test_answer_callback_query_without_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": True})

    client, requests = _client(handler)
    assert await client.answer_callback_query(callback_query_id="cb-1") is True
    assert b'"text"' not in requests[0].content


async def test_edit_message_text_targets_the_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/editMessageText")
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 9, "chat": {"id": 1}}})

    client, _ = _client(handler)
    sent = await client.edit_message_text(chat_id=1, message_id=9, text="actualizado")
    assert sent.message_id == 9


# --------------------------------------------------------------------------- #
# getFile: multipart download, never JSON
# --------------------------------------------------------------------------- #
async def test_get_file_downloads_bytes_from_the_file_endpoint() -> None:
    payload = b"%PDF-1.7 fake bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {"file_id": "f1", "file_path": "documents/file_1.pdf", "file_size": len(payload)},
                },
            )
        assert request.url.path == f"/file/bot{TOKEN}/documents/file_1.pdf"
        return httpx.Response(200, content=payload)

    client, requests = _client(handler)
    downloaded = await client.get_file(file_id="f1")

    assert downloaded.content == payload
    assert downloaded.file_path == "documents/file_1.pdf"
    assert len(requests) == 2, "getFile resolves the path, then the file is downloaded separately"


async def test_get_file_refuses_a_file_above_the_cap_before_downloading() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"file_id": "big", "file_path": "big.pdf", "file_size": 10_000},
            },
        )

    client, requests = _client(handler)
    with pytest.raises(TelegramApiError) as excinfo:
        await client.get_file(file_id="big", max_bytes=100)
    assert excinfo.value.context["file_size"] == 10_000
    assert len(requests) == 1, "the download must not start when the declared size is over the cap"


async def test_get_file_aborts_a_stream_that_exceeds_the_cap() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return httpx.Response(
                200, json={"ok": True, "result": {"file_id": "x", "file_path": "p"}}
            )
        return httpx.Response(200, content=b"x" * 500)

    client, _ = _client(handler)
    with pytest.raises(TelegramApiError, match="exceeded the configured cap"):
        await client.get_file(file_id="x", max_bytes=100)


async def test_get_file_without_a_path_is_an_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"file_id": "x"}})

    client, _ = _client(handler)
    with pytest.raises(TelegramApiError, match="no file_path"):
        await client.get_file(file_id="x")


# --------------------------------------------------------------------------- #
# Webhook management
# --------------------------------------------------------------------------- #
async def test_set_webhook_sends_the_secret_and_allowed_updates() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content.decode()))
        return httpx.Response(200, json={"ok": True, "result": True})

    client, _ = _client(handler)
    await client.set_webhook(
        url="https://example.invalid/telegram/webhook",
        secret_token="s3cret",
        allowed_updates=["message", "edited_message", "callback_query"],
    )
    assert captured["secret_token"] == "s3cret"
    assert captured["allowed_updates"] == ["message", "edited_message", "callback_query"]


async def test_get_webhook_info_returns_the_object() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "result": {"url": "https://x/y", "last_error_message": None}},
        )

    client, _ = _client(handler)
    info = await client.get_webhook_info()
    assert info["url"] == "https://x/y"
