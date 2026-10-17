"""Delivery of messages to the user (PLAN.md §2.1, §5.2).

Layer 3. Imports layer 0-2.

The dispatcher needs to *send* a reminder. Two transports exist:

* ``telegram`` — the real Bot API, the only one allowed in production;
* ``record`` — appends one JSON line per send to a file.

The second exists because the concurrency acceptance criterion is "two concurrent dispatcher
runs send **exactly one** reminder", and proving that from outside the process requires an
observation point the test can read after both processes have exited. Reading the ledger alone
would be circular: it would show what the code believed, not what it attempted to deliver.
``NOTIFICATIONS_TRANSPORT=record` is a production startup error, the same pattern as
``STORE_BACKEND=memory`` and ``EMBED_PROVIDER=stub``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.errors import AssistantError
from app.logging_config import redact

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """What a delivery attempt produced."""

    delivered: bool
    message_id: int | None = None
    error: str | None = None

    @property
    def transport_failed(self) -> bool:
        """True when the failure is a transport error rather than a logical refusal."""

        return not self.delivered and self.error is not None


@runtime_checkable
class Notifier(Protocol):
    """Sends one message to one chat."""

    async def send(self, *, chat_id: int, text: str) -> DeliveryResult:
        ...


@runtime_checkable
class MessageSender(Protocol):
    """Anything with `send_message(chat_id=..., text=...)`.

    Declared structurally so this layer does not import `app.bot` (PLAN.md §2.2: services may
    not import the transport). `main.py` and the job inject the real `TelegramClient`.
    """

    async def send_message(self, *, chat_id: int, text: str) -> Any:
        ...


class TelegramNotifier:
    """Delivers through the Bot API."""

    def __init__(self, client: MessageSender) -> None:
        self._client = client

    async def send(self, *, chat_id: int, text: str) -> DeliveryResult:
        try:
            sent = await self._client.send_message(chat_id=chat_id, text=text)
        except AssistantError as exc:
            _logger.warning("delivery failed", extra=exc.as_log_fields())
            return DeliveryResult(delivered=False, error=exc.code)
        return DeliveryResult(delivered=True, message_id=sent.message_id)


class RecordingNotifier:
    """Writes each send to a file as one JSON line. Testing only."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    async def send(self, *, chat_id: int, text: str) -> DeliveryResult:
        record = {"chat_id": chat_id, "text": redact(text, key="content")}
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        _logger.info("delivery recorded", extra={"chat_id": chat_id, "status": "recorded"})
        # A synthetic id: nothing was really delivered, and the caller must not pretend it was.
        return DeliveryResult(delivered=True, message_id=0)


def build_notifier(settings, telegram: MessageSender | None) -> Notifier:
    """Choose the transport from configuration."""

    if settings.notifications_transport == "record":
        if settings.is_production:  # pragma: no cover - refused by configuration validation
            raise ValueError("NOTIFICATIONS_TRANSPORT=record is not allowed in production")
        return RecordingNotifier(settings.notify_record_path)
    if telegram is None:
        raise ValueError("the telegram notifier needs a message sender")
    return TelegramNotifier(telegram)


__all__ = [
    "DeliveryResult",
    "MessageSender",
    "Notifier",
    "RecordingNotifier",
    "TelegramNotifier",
    "build_notifier",
]
