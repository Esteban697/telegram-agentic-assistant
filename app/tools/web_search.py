"""Web search behind a configurable provider (spec §10, PLAN.md §13.2).

Layer 4. With no provider configured it returns a clear capability error rather than invented
data: spec §10 is explicit that a missing provider is a capability gap, not something to guess
around. The provider port is a plain callable so Phase 4/5 can bind a real search API without
touching this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from app.policy.risk import RiskLevel
from app.tools.base import ToolResult
from app.tools.registry import ToolSpec

NO_PROVIDER = "CapabilityUnavailable"


class SearchWebArgs(BaseModel):
    query: str = Field(min_length=1, max_length=200)


@dataclass
class WebSearchTool:
    """`provider` must expose `async search(query) -> list[dict]`."""

    provider: Any | None = None

    async def search_web(self, *, user_id: int, args: SearchWebArgs, **_: Any) -> ToolResult[Any]:
        if self.provider is None:
            return ToolResult.failure(
                NO_PROVIDER,
                message_key="error.capability_unavailable",
                detail="no web-search provider is configured",
                capability="search_web",
            )
        results = await self.provider.search(args.query)  # pragma: no cover - no provider yet
        return ToolResult.success(results)


def specs(tool: WebSearchTool) -> list[ToolSpec]:
    return [
        ToolSpec(
            name="search_web",
            description=(
                "Search the web for a query. Returns a capability error until a provider is "
                "configured."
            ),
            args_model=SearchWebArgs,
            risk_level=RiskLevel.READ,
            timeout_seconds=15.0,
            handler=tool.search_web,
        )
    ]


__all__ = ["NO_PROVIDER", "SearchWebArgs", "WebSearchTool", "specs"]
