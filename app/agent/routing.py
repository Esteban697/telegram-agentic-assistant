"""Routing for the Phase 3 graph (PLAN.md §2.1, §10 Phase 3).

Four destinations, decided from the state rather than from a prompt: an answer, a tool round,
an approval hand-off, or a controlled failure. `clarify` is expressed as the *answer* branch with
no tool calls — the model asks the user for the missing datum — which is why it needs no node of
its own.
"""

from __future__ import annotations

from app.agent.state import ChatState

ANSWER = "answer"
TOOLS = "tools"
APPROVAL = "approval"
FAILURE = "failure"


def route_after_model(state: ChatState) -> str:
    """After the model: fail, hand off for approval, run tools, or answer."""

    if state.get("error_code"):
        return FAILURE
    return TOOLS if state.get("tool_calls") else ANSWER


def route_after_tools(state: ChatState, *, max_iterations: int) -> str:
    """After a tool round: stop for an approval, stop at the cap, or ask the model again."""

    if state.get("pending_approval"):
        return APPROVAL
    if int(state.get("iterations") or 0) >= max_iterations:
        return APPROVAL if state.get("pending_approval") else ANSWER
    return TOOLS


__all__ = ["ANSWER", "APPROVAL", "FAILURE", "TOOLS", "route_after_model", "route_after_tools"]
