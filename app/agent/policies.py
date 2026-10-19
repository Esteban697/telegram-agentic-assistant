"""Graph-side policy glue (PLAN.md §2.1, §4.3).

Layer 5. It answers two questions the graph asks about a tool call: *may this run now* (level
0/1) and *does this need the user's approval first* (level 2), and it turns the second into the
frozen :class:`~app.services.approvals.ApprovalRequest` the bot renders.

The decision itself lives in :mod:`app.policy.risk`; this module only applies it to a call. The
model's opinion is never consulted: spec §7 puts the policy in code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.errors import AssistantError
from app.policy.risk import RiskLevel, assert_enabled, requires_approval
from app.services.approvals import ApprovalRequest
from app.tools.registry import ToolRegistry


class ToolNotAllowed(AssistantError):
    """The tool exists but its level forbids running it (level 3 in the MVP)."""

    code = "tool_not_allowed"
    message_key = "error.tool_not_allowed"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """What the graph should do with one tool call."""

    tool_name: str
    level: RiskLevel
    allowed_now: bool
    needs_approval: bool

    @property
    def blocked(self) -> bool:
        return not self.allowed_now and not self.needs_approval


def decide(registry: ToolRegistry, tool_name: str) -> PolicyDecision:
    """The policy table applied to one call."""

    level = registry.level_of(tool_name)
    try:
        assert_enabled(level)
    except Exception as exc:  # level 3
        raise ToolNotAllowed(str(exc), context={"tool_name": tool_name}) from exc
    return PolicyDecision(
        tool_name=tool_name,
        level=level,
        allowed_now=not requires_approval(level),
        needs_approval=requires_approval(level),
    )


def preview_text_for(tool_name: str, arguments: dict[str, Any]) -> str:
    """A frozen, human-readable summary of exactly what would run.

    It is stored with the action (`preview_text`) and shown on the button, so the user approves
    a concrete description rather than a category. It is capped below Telegram's 4096-character
    message limit: a calendar description may be long, and a rejected approval prompt would be
    worse than a truncated one.
    """

    rendered = ", ".join(f"{key}={value!r}" for key, value in sorted(arguments.items()))
    text = f"{tool_name}({rendered})"
    if len(text) > 3000:
        text = text[:2999] + "…"
    return text


async def prepare_approval(
    approvals: Any,
    *,
    user_id: int,
    chat_id: int,
    tool_name: str,
    arguments: dict[str, Any],
    validator: Any | None = None,
) -> ApprovalRequest:
    """Create the `pending` action the approval prompt is built from.

    `validator` is the tool's own argument validation, supplied by the graph: spec §10 requires a
    schema and validation for every tool, and level-2 arguments used to be stored unchecked.
    """

    return await approvals.prepare(
        user_id=user_id,
        chat_id=chat_id,
        action_type=tool_name,
        args=arguments,
        preview_text=preview_text_for(tool_name, arguments),
        validator=validator,
    )


__all__ = [
    "PolicyDecision",
    "ToolNotAllowed",
    "decide",
    "prepare_approval",
    "preview_text_for",
]
