"""The LLM ports (PLAN.md §2.3.2). Layer 0: no ``app`` imports.

This module is the **only seam** through which the rest of the application learns the shape
of a model. ``agent/`` depends on the :class:`ChatModel` protocol and never on
``app.llm.ollama_client``, which is what makes the spec's "permitir cambiar ``LLM_MODEL``
sin cambiar la lógica de negocio" (§3) and the provider-agnostic tools requirement (§10)
mechanically true rather than aspirational.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

ChatRole = str  # "system" | "user" | "assistant" | "tool"


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One conversation turn, provider-independent."""

    role: ChatRole
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool the model may call, described as JSON Schema.

    Mirrors the shape Ollama documents for ``/api/chat`` ``tools[]``: the caller passes the
    JSON Schema in :attr:`parameters` and the client wraps it in the
    ``{"type": "function", "function": {...}}`` envelope.
    """

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A tool call returned by the model."""

    name: str
    arguments: dict[str, Any]
    call_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChatResult:
    """What a chat call returns to the business layer."""

    content: str
    model: str
    tool_calls: tuple[ToolCall, ...] = ()
    latency_ms: float | None = None
    duration_ms: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


@runtime_checkable
class ChatModel(Protocol):
    """The chat port every provider adapter implements."""

    model: str

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        response_format: str | dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> ChatResult:
        """Return one assistant turn."""
        ...

    async def list_models(self) -> list[str]:
        """Return the model names the endpoint advertises."""
        ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """The embeddings port.

    Declared in Phase 1 because PLAN.md §10 Phase 1 deliverable 4 lists it as part of the
    ports module; the first implementation arrives in Phase 4 together with the decision
    recorded in §13.1. Nothing in Phase 1 calls it — the protocol exists so that the seam
    is defined before the adapter, which is what the plan's staging requires.
    """

    model: str
    dim: int

    async def embed(self, texts: list[str], *, timeout: float | None = None) -> list[list[float]]:
        """Return one vector per input text."""
        ...


__all__ = [
    "ChatMessage",
    "ChatModel",
    "ChatResult",
    "ChatRole",
    "EmbeddingProvider",
    "ToolCall",
    "ToolSpec",
]
