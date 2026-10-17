"""Trivial routing for the Phase 1 graph (PLAN.md §10 Phase 1 deliverable 9).

One node, so the only decision is "the model answered" or "the model failed". It exists as a
separate module because Phase 3 replaces it with the real routing (answer / tool call /
approval / clarify / hand-off) and the graph should not have to move to accommodate that.
"""

from __future__ import annotations

from app.agent.state import ChatState

DONE = "done"
FAILED = "failed"


def route_after_model(state: ChatState) -> str:
    """Return the next node name after the model node."""

    return FAILED if state.get("error_code") else DONE


__all__ = ["DONE", "FAILED", "route_after_model"]
