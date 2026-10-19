"""Current date and time for relative-date reasoning (level 0).

The model has no clock. Calendar requests like "hoy", "mañana" or "el jueves" need the actual
local date, not the model's training date, so this cheap read tool returns it in the configured
IANA zone.

Layer 4. No I/O beyond reading the process clock, and the clock is injectable for tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from pydantic import BaseModel

from app.policy.risk import RiskLevel
from app.policy.timezones import load_zone
from app.tools.base import ToolResult
from app.tools.registry import ToolSpec

_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


class EmptyArgs(BaseModel):
    """No arguments: the tool reports the server's current instant."""


@dataclass
class ClockTool:
    timezone: str = "Europe/Madrid"
    now: Callable[[], datetime] | None = None

    async def get_current_datetime(
        self, *, user_id: int, args: EmptyArgs | None = None, **_: Any
    ) -> ToolResult[dict[str, Any]]:
        moment = self.now() if self.now is not None else datetime.now(UTC)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        zone = load_zone(self.timezone)
        local = moment.astimezone(zone)
        return ToolResult.success(
            {
                "now_utc": moment.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "local": local.isoformat(),
                "date": local.strftime("%Y-%m-%d"),
                "time": local.strftime("%H:%M"),
                "weekday": _WEEKDAYS[local.weekday()],
                "weekday_index": local.weekday(),
                "timezone": self.timezone,
            }
        )


def specs(tool: ClockTool) -> list[ToolSpec]:
    return [
        ToolSpec(
            name="get_current_datetime",
            description=(
                "Return the current local date, time and weekday in the configured time zone. "
                "Call it before interpreting any relative date."
            ),
            args_model=EmptyArgs,
            risk_level=RiskLevel.READ,
            timeout_seconds=5.0,
            handler=tool.get_current_datetime,
        )
    ]


__all__ = ["ClockTool", "EmptyArgs", "specs"]
