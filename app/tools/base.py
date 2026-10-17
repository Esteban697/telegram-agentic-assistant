"""Shared tool plumbing: argument validation, timeouts, structured results.

Layer 4. Spec §10: "Todas las herramientas deben tener esquemas de argumentos, validación,
timeout, límites, manejo de errores y resultados estructurados."

A tool is a plain typed function over a pydantic argument model, returning a
:class:`ToolResult`. Nothing here knows about the graph: Phase 3 binds these into the registry
and the graph; Phase 2 calls them from the command handlers. Keeping the shape stable now is
what lets Phase 3 add the registry without rewriting the tools.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from app.errors import AssistantError, CapabilityUnavailable

_logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ToolResult(Generic[T]):
    """A structured outcome: never a bare string, never a raised provider error."""

    ok: bool
    data: T | None = None
    error_code: str | None = None
    message_key: str = "error.generic"
    detail: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, data: T, **meta: Any) -> "ToolResult[T]":
        return cls(ok=True, data=data, meta=meta)

    @classmethod
    def failure(
        cls, error_code: str, *, message_key: str = "error.generic", detail: str = "", **meta: Any
    ) -> "ToolResult[T]":
        return cls(ok=False, error_code=error_code, message_key=message_key, detail=detail, meta=meta)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Everything the registry (Phase 3) needs to expose a tool to the model."""

    name: str
    description: str
    args_model: type[BaseModel]
    risk_level: int  # 0 read, 1 reversible internal, 2 external commitment, 3 high impact


def validate_args(model: type[BaseModel], raw: dict[str, Any]) -> BaseModel:
    """Validate arguments, turning pydantic's error into our taxonomy.

    A model-supplied argument that fails validation raises a typed error the caller reports;
    it must never be silently coerced into something executable.
    """

    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise AssistantError(
            f"invalid arguments for {model.__name__}",
            context={"validation_errors": len(exc.errors())},
        ) from exc


__all__ = ["ToolResult", "ToolSpec", "validate_args"]
