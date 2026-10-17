"""Graph state for the Phase 3 graph (PLAN.md §2.1, §10 Phase 3).

`turn` is the load-bearing addition: `run_key` is composed from it, and it is *checkpointed
state*, so it is identical when a node is replayed and different for a new user turn. That is
what makes §5.3's four cases behave correctly.
"""

from __future__ import annotations

from typing import Any, TypedDict

from app.llm.protocols import ChatMessage


class ToolCallView(TypedDict, total=False):
    """A tool call as the graph carries it: what the model asked for, and where it sat."""

    tool_name: str
    arguments: dict[str, Any]
    call_id: str
    call_index: int


class ChatState(TypedDict, total=False):
    """State passed through the graph."""

    messages: list[dict[str, Any]]
    reply: str
    model: str
    error_code: str | None
    latency_ms: float | None
    # Monotonic per thread, incremented once per user turn. Part of the ledger key.
    turn: int
    # Identity and thread, carried in the state so nodes need no config plumbing.
    thread_id: str
    user_id: int
    chat_id: int
    # Tool calls produced by the current model turn.
    tool_calls: list[ToolCallView]
    tool_results: list[dict[str, Any]]
    # Set when a level-2 action was prepared and is waiting for the user.
    pending_approval: dict[str, Any] | None
    iterations: int


def to_message_dicts(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    """Convert protocol messages into the plain dicts the graph carries."""

    return [message.as_dict() for message in messages]


def from_message_dicts(raw: list[dict[str, Any]]) -> list[ChatMessage]:
    """Convert graph-carried dicts back into protocol messages."""

    return [
        ChatMessage(role=str(entry.get("role", "user")), content=str(entry.get("content", "")))
        for entry in raw
    ]


__all__ = ["ChatState", "ToolCallView", "from_message_dicts", "to_message_dicts"]
