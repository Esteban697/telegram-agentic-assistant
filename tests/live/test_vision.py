"""Live check: the Ollama Cloud vision model reads an image + text turn.

Skipped without an API key. It sends a generated red square and expects a colour word, which is
the smallest end-to-end proof that the cloud model accepts the `images` field.
"""

from __future__ import annotations

import struct
import zlib

import pytest

from app.agent.prompts import VISION_SYSTEM_PROMPT
from app.config import load_settings
from app.llm.ollama_client import OllamaChatClient
from app.llm.protocols import ChatMessage

pytestmark = pytest.mark.live


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def _red_png(size: int = 128) -> bytes:
    raw = b"".join(b"\x00" + bytes([255, 0, 0]) * size for _ in range(size))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


async def test_the_cloud_vision_model_reads_an_image() -> None:
    settings = load_settings()
    if not settings.api_key:
        pytest.skip("OLLAMA_API_KEY is not configured")

    client = OllamaChatClient(
        base_url=settings.ollama_base_url,
        model=settings.vision_model,
        api_key=settings.api_key,
        timeout=60.0,
        max_retries=0,
    )
    try:
        result = await client.chat(
            [
                ChatMessage(role="system", content=VISION_SYSTEM_PROMPT),
                ChatMessage(
                    role="user",
                    content="What colour is this image? Answer with one word.",
                    images=(_red_png(),),
                ),
            ],
            tools=None,
        )
    finally:
        await client.aclose()

    assert result.content.strip(), "the vision model returned an empty answer"
    assert any(
        word in result.content.lower() for word in ("red", "rojo", "roja")
    ), f"unexpected vision answer: {result.content!r}"
