"""Restaurant search, availability and the level-2 reservation action (spec §10, PLAN.md §13.3).

Layer 4. Two of the three tools are read-only adapters that report a capability error until a
real provider is configured; the third is the **level-2 action** the approval machinery is
exercised with.

`submit_reservation` performs nothing, by design: spec §2 puts real bookings out of scope and
§7 forbids claiming a booking happened without verifiable provider confirmation. The approval
path around it is real, which is what makes acceptance (a) provable with no external provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from app.policy.risk import RiskLevel
from app.tools.base import ToolResult
from app.tools.registry import ToolSpec

NO_PROVIDER = "CapabilityUnavailable"


class SearchRestaurantsArgs(BaseModel):
    criteria: str = Field(min_length=1, max_length=200)
    city: str | None = Field(default=None, max_length=80)


class AvailabilityArgs(BaseModel):
    restaurant: str = Field(min_length=1, max_length=160)
    party_size: int = Field(ge=1, le=20)
    local_time: str = Field(min_length=8, max_length=32)


class ReservationArgs(BaseModel):
    """The arguments of the level-2 action, so the approval prompt shows exactly what would run."""

    restaurant: str = Field(min_length=1, max_length=160)
    party_size: int = Field(ge=1, le=20)
    local_time: str = Field(min_length=8, max_length=32)
    name: str = Field(min_length=1, max_length=120)


@dataclass
class RestaurantTools:
    """`provider` must expose `async search(criteria, city)` and `async check(...)`."""

    provider: Any | None = None

    async def search_restaurants(
        self, *, user_id: int, args: SearchRestaurantsArgs, **_: Any
    ) -> ToolResult[Any]:
        if self.provider is None:
            return ToolResult.failure(
                NO_PROVIDER,
                message_key="error.capability_unavailable",
                detail="no restaurant provider is configured",
                capability="search_restaurants",
            )
        return ToolResult.success(await self.provider.search(args.criteria, args.city))  # pragma: no cover

    async def check_restaurant_availability(
        self, *, user_id: int, args: AvailabilityArgs, **_: Any
    ) -> ToolResult[Any]:
        if self.provider is None:
            return ToolResult.failure(
                NO_PROVIDER,
                message_key="error.capability_unavailable",
                detail="no availability provider is configured",
                capability="check_restaurant_availability",
            )
        # A real answer must carry its source and the time it was checked (spec §10).
        availability = await self.provider.check(  # pragma: no cover - no provider yet
            args.restaurant, args.party_size, args.local_time
        )
        return ToolResult.success(availability)

    async def submit_reservation(
        self, *, user_id: int, args: ReservationArgs, **_: Any
    ) -> ToolResult[Any]:
        """Never performed: no provider, and the result says so instead of implying success."""

        return ToolResult.failure(
            NO_PROVIDER,
            message_key="error.reservation_not_supported",
            detail=(
                "no booking provider is integrated; the reservation was prepared and approved "
                "but cannot be submitted"
            ),
            capability="submit_reservation",
        )


def specs(tool: RestaurantTools) -> list[ToolSpec]:
    return [
        ToolSpec(
            name="search_restaurants",
            description="Look for restaurants matching criteria. Adapter only.",
            args_model=SearchRestaurantsArgs,
            risk_level=RiskLevel.READ,
            timeout_seconds=15.0,
            handler=tool.search_restaurants,
        ),
        ToolSpec(
            name="check_restaurant_availability",
            description=(
                "Check availability with a real provider; unavailable until one is configured."
            ),
            args_model=AvailabilityArgs,
            risk_level=RiskLevel.READ,
            timeout_seconds=15.0,
            handler=tool.check_restaurant_availability,
        ),
        ToolSpec(
            name="submit_reservation",
            description=(
                "Submit a restaurant reservation. Requires explicit user approval; no booking "
                "provider is integrated, so execution reports the capability as unavailable."
            ),
            args_model=ReservationArgs,
            risk_level=RiskLevel.EXTERNAL,
            timeout_seconds=20.0,
            handler=tool.submit_reservation,
        ),
    ]


__all__ = [
    "AvailabilityArgs",
    "NO_PROVIDER",
    "ReservationArgs",
    "RestaurantTools",
    "SearchRestaurantsArgs",
    "specs",
]
