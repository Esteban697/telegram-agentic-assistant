"""Provider error taxonomy — a **reference**, not a second definition.

The classes this module exposes (``OllamaTimeout``, ``OllamaRateLimited``,
``OllamaUnavailable``, ``OllamaBadResponse``, ``OllamaUnsupportedModel`` and their base
``OllamaError``) are defined once in :mod:`app.errors`, which is the single authority for the
whole taxonomy. This module exists because PLAN.md §2.1 lists it as the provider-facing
import surface, and it is a re-export on purpose: duplicating the definitions here is exactly
the drift §15 exists to prevent.
"""

from __future__ import annotations

from app.errors import (
    OllamaBadResponse,
    OllamaError,
    OllamaRateLimited,
    OllamaTimeout,
    OllamaUnavailable,
    OllamaUnsupportedModel,
)

__all__ = [
    "OllamaBadResponse",
    "OllamaError",
    "OllamaRateLimited",
    "OllamaTimeout",
    "OllamaUnavailable",
    "OllamaUnsupportedModel",
]
