"""Image messages: Telegram photo → cloud vision model → reply.

The vision model and the conversation service are fakes; the handler, the photo selection, the
MIME sniffing, the recording and the message catalogue are the real ones.
"""

from __future__ import annotations

from typing import Any

from app.bot.handlers import TelegramHandlers
from app.bot.messages import render
from app.bot.telegram_client import DownloadedFile
from app.errors import TelegramApiError
from app.llm.protocols import ChatMessage, ChatResult
from app.policy.authz import TelegramIdentity
from app.services.conversation import PreparedTurn
from tests.conftest import RecordingTelegram, make_settings

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-payload"


class FakeTelegram(RecordingTelegram):
    def __init__(self, *, content: bytes = PNG, error: Exception | None = None) -> None:
        super().__init__()
        self.content = content
        self.error = error
        self.get_file_kwargs: dict[str, Any] | None = None

    async def get_file(self, **kwargs: Any) -> DownloadedFile:
        self.get_file_kwargs = kwargs
        if self.error is not None:
            raise self.error
        return DownloadedFile(
            file_id=str(kwargs.get("file_id")),
            file_path="photos/file.jpg",
            file_size=len(self.content),
            content=self.content,
        )


class FakeConversations:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    async def prepare(self, *, user_id: int, chat_id: int, text: str) -> PreparedTurn:
        return PreparedTurn(
            thread_id="thread-1",
            prompt_input=[ChatMessage(role="user", content=text)],
            history_length=0,
        )

    async def conversation_id(self, *, user_id: int, chat_id: int) -> str:
        return "conversation-1"

    async def record(self, **kwargs: Any) -> None:
        self.recorded.append(kwargs)


class FakeVision:
    model = "gemma4:31b"

    def __init__(self, content: str = "Es roja.") -> None:
        self.content = content
        self.calls: list[tuple[list[ChatMessage], dict[str, Any]]] = []

    async def chat(self, messages: list[ChatMessage], **kwargs: Any) -> ChatResult:
        self.calls.append((messages, kwargs))
        return ChatResult(content=self.content, model=self.model, duration_ms=12.0)

    async def list_models(self) -> list[str]:
        return [self.model]


def _handlers(*, telegram: FakeTelegram, vision: Any) -> tuple[TelegramHandlers, FakeConversations]:
    conversations = FakeConversations()
    handlers = TelegramHandlers(
        settings=make_settings(max_image_mb=1, vision_timeout_seconds=5.0),
        telegram=telegram,  # type: ignore[arg-type]
        agent=None,  # type: ignore[arg-type]
        conversations=conversations,  # type: ignore[arg-type]
        vision_model=vision,
    )
    return handlers, conversations


def _photo_update(*, caption: str | None = "¿De qué color es?") -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": 1,
        "from": {"id": 42},
        "chat": {"id": 4242},
        "photo": [
            {"file_id": "small", "file_size": 10, "width": 10, "height": 10},
            {"file_id": "large", "file_size": 100, "width": 100, "height": 100},
        ],
    }
    if caption is not None:
        message["caption"] = caption
    return {"update_id": 1, "message": message}


async def test_a_photo_is_answered_by_the_vision_model() -> None:
    telegram = FakeTelegram()
    vision = FakeVision()
    handlers, conversations = _handlers(telegram=telegram, vision=vision)

    outcome = await handlers.handle(_photo_update(), TelegramIdentity(user_id=42, chat_id=4242))

    assert outcome.status == "processed"
    assert telegram.calls[-1].kwargs["text"] == "Es roja."
    assert telegram.get_file_kwargs == {"file_id": "large", "max_bytes": 1_048_576}
    assert conversations.recorded[-1]["user_text"] == "[imagen] ¿De qué color es?"
    assert conversations.recorded[-1]["assistant_text"] == "Es roja."

    messages, kwargs = vision.calls[0]
    assert isinstance(messages[0], ChatMessage) and messages[0].role == "system"
    user_messages = [m for m in messages if m.role == "user" and m.images]
    assert len(user_messages) == 1
    assert user_messages[0].images == (PNG,)
    assert user_messages[0].content == "¿De qué color es?"
    assert kwargs["tools"] is None


async def test_a_photo_without_caption_uses_the_default_question() -> None:
    telegram = FakeTelegram()
    vision = FakeVision()
    handlers, _ = _handlers(telegram=telegram, vision=vision)

    await handlers.handle(_photo_update(caption=None), TelegramIdentity(user_id=42, chat_id=4242))

    messages, _ = vision.calls[0]
    user_messages = [m for m in messages if m.role == "user" and m.images]
    assert user_messages[0].content == "Describe la imagen y dime lo que ves."


async def test_a_non_image_payload_is_refused() -> None:
    telegram = FakeTelegram(content=b"not-an-image")
    handlers, _ = _handlers(telegram=telegram, vision=FakeVision())

    outcome = await handlers.handle(_photo_update(), TelegramIdentity(user_id=42, chat_id=4242))

    assert outcome.detail == "unsupported_image"
    assert telegram.calls[-1].kwargs["text"] == render("images.unsupported")


async def test_an_oversized_image_gets_the_size_message() -> None:
    telegram = FakeTelegram(
        error=TelegramApiError("file is 2000000 bytes, above the 1048576-byte cap")
    )
    handlers, _ = _handlers(telegram=telegram, vision=FakeVision())

    outcome = await handlers.handle(_photo_update(), TelegramIdentity(user_id=42, chat_id=4242))

    assert outcome.status == "failed"
    assert telegram.calls[-1].kwargs["text"] == render("images.too_large", max_mb=1)


async def test_without_a_vision_model_the_handler_says_unavailable() -> None:
    telegram = FakeTelegram()
    handlers, _ = _handlers(telegram=telegram, vision=None)

    outcome = await handlers.handle(_photo_update(), TelegramIdentity(user_id=42, chat_id=4242))

    assert outcome.status == "ignored"
    assert telegram.calls[-1].kwargs["text"] == render("images.unavailable")
