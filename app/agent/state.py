"""Graph state for the Phase 1 single-node graph (PLAN.md §10 Phase 1 deliverable 9).

Deliberately minimal: a message list in, a reply out. Phase 2 attaches the checkpointer to
this same state rather than restructuring, and Phase 3 adds the tool/approval fields.
"""

from __future__ import annotations

from typing import Any, TypedDict

from app.llm.protocols import ChatMessage


class ChatState(TypedDict, total=False):
    """State passed through the graph.

    ``messages`` is the **shaped prompt input** assembled by the handler: history (from the
    store) plus this turn's message. It may be a plain dict rather than a
    :class:`~app.llm.protocols.ChatMessage` because it must survive LangGraph's serialisation
    once a checkpointer is attached in Phase 2.
    """

    messages: list[dict[str, Any]]
    reply: str
    model: str
    error_code: str | None
    latency_ms: float | None


def to_message_dicts(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    """Convert protocol messages into the plain dicts the graph carries."""

    return [message.as_dict() for message in messages]


def from_message_dicts(raw: list[dict[str, Any]]) -> list[ChatMessage]:
    """Convert graph-carried dicts back into protocol messages."""

    return [
        ChatMessage(role=str(entry.get("role", "user")), content=str(entry.get("content", "")))
        for entry in raw
    ]


__all__ = ["ChatState", "from_message_dicts", "to_message_dicts"]
