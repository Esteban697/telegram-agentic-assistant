"""Shared tool plumbing: argument validation and structured results.

The `ToolSpec` that lived here in Phase 2 moved to :mod:`app.tools.registry`, which PLAN.md §2.1
declares the single source of tool truth; this module keeps the two helpers both layers need.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from app.errors import InvalidArguments

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


def validate_args(model: type[BaseModel], raw: dict[str, Any]) -> BaseModel:
    """Validate arguments, turning pydantic's error into our taxonomy.

    A model-supplied argument that fails validation raises a typed error the caller reports; it
    must never be silently coerced into something executable.
    """

    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise InvalidArguments(
            f"invalid arguments for {model.__name__}",
            context={"validation_errors": len(exc.errors())},
        ) from exc


__all__ = ["ToolResult", "validate_args"]
