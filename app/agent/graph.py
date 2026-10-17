"""The Phase 1 graph: one node that calls the model (PLAN.md §10 Phase 1 deliverable 9).

LangGraph is used from Phase 1 on purpose, so that Phase 2 attaches the checkpointer to this
graph instead of restructuring it. The graph depends on the :class:`~app.llm.protocols.ChatModel`
**protocol** and receives a concrete instance by constructor injection from ``main.py``
(§2.2: ``agent/`` never imports HTTP provider code).

Two Phase 1 requirements are implemented here:

* ``REQUEST_BUDGET_SECONDS`` is applied as a hard timeout around the graph call (NN8). The
  queue hand-off that reuses the same variable arrives in Phase 3 (§5.5).
* Provider failures are **caught inside the node** and turned into a state field, so a
  provider exception cannot escape the graph and skip whatever bookkeeping the caller needs.
  The agent then re-raises the mapped typed error, which is what the handler renders from the
  Spanish catalogue.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from langgraph.graph import END, START, StateGraph

from app.agent.routing import DONE, FAILED, route_after_model
from app.agent.state import ChatState
from app.errors import AssistantError, BudgetExceeded, OllamaBadResponse
from app.llm.protocols import ChatMessage, ChatModel, ChatResult

_logger = logging.getLogger(__name__)

MODEL_NODE = "call_model"
FAILURE_NODE = "report_failure"


class ChatAgent:
    """Runs one conversational turn through the graph."""

    def __init__(
        self,
        model: ChatModel,
        *,
        system_prompt: str,
        budget_seconds: float,
    ) -> None:
        self._model = model
        self._system_prompt = system_prompt
        self._budget_seconds = budget_seconds
        self._graph = self._build_graph()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    async def run(self, messages: Sequence[ChatMessage]) -> ChatResult:
        """Run a turn over the already-shaped prompt input.

        ``messages`` is supplied by the caller (the handler), never read from a store here.
        """

        shaped = self._shape(messages)
        initial: ChatState = {"messages": shaped}

        try:
            async with asyncio.timeout(self._budget_seconds):
                final_state = await self._graph.ainvoke(initial)
        except TimeoutError as exc:
            # Raised as a typed error so the handler picks the Spanish timeout reply.
            raise BudgetExceeded(
                f"turn exceeded REQUEST_BUDGET_SECONDS={self._budget_seconds}",
                context={"budget_seconds": self._budget_seconds},
            ) from exc

        error_code = final_state.get("error_code")
        if error_code:
            raise self._error_for(error_code)

        return ChatResult(
            content=str(final_state.get("reply", "")),
            model=str(final_state.get("model") or self._model.model),
            latency_ms=final_state.get("latency_ms"),
        )

    @property
    def model_name(self) -> str:
        """The configured model name, straight from the injected port."""

        return self._model.model

    # ------------------------------------------------------------------ #
    # Graph
    # ------------------------------------------------------------------ #
    def _build_graph(self):  # noqa: ANN202 - compiled graph has no public type
        builder = StateGraph(ChatState)
        builder.add_node(MODEL_NODE, self._call_model)
        builder.add_node(FAILURE_NODE, self._report_failure)
        builder.add_edge(START, MODEL_NODE)
        builder.add_conditional_edges(
            MODEL_NODE,
            route_after_model,
            {DONE: END, FAILED: FAILURE_NODE},
        )
        builder.add_edge(FAILURE_NODE, END)
        return builder.compile()

    def _shape(self, messages: Sequence[ChatMessage]) -> list[dict[str, str]]:
        shaped: list[dict[str, str]] = [{"role": "system", "content": self._system_prompt}]
        shaped.extend(message.as_dict() for message in messages)
        return shaped

    async def _call_model(self, state: ChatState) -> ChatState:
        messages = [
            ChatMessage(role=str(entry.get("role", "user")), content=str(entry.get("content", "")))
            for entry in state.get("messages", [])
        ]
        try:
            result = await self._model.chat(messages)
        except AssistantError as exc:
            # Deliberately not re-raised: the graph records the failure and the agent maps
            # it back to a typed error, so a Phase 2 checkpointer still sees a terminal state.
            _logger.warning("model call failed", extra=exc.as_log_fields())
            return {"error_code": exc.code}
        except Exception as exc:  # pragma: no cover - unexpected provider bug
            _logger.error(
                "unexpected model failure",
                extra={"error_code": "unexpected_model_error"},
                exc_info=exc,
            )
            return {"error_code": OllamaBadResponse().code}

        _logger.info(
            "model turn complete",
            extra={
                "model": result.model,
                "duration_ms": round(result.latency_ms or 0.0, 1),
                "status": "ok",
            },
        )
        return {
            "reply": result.content,
            "model": result.model,
            "latency_ms": result.latency_ms,
            "error_code": None,
        }

    @staticmethod
    def _report_failure(state: ChatState) -> ChatState:
        """Terminal marker node. Phase 3 turns this into the notify/hand-off path."""

        return {"error_code": state.get("error_code")}

    @staticmethod
    def _error_for(code: str) -> AssistantError:
        from app import errors  # local import keeps the module surface small

        for candidate in (
            errors.OllamaTimeout,
            errors.OllamaRateLimited,
            errors.OllamaUnavailable,
            errors.OllamaUnsupportedModel,
            errors.OllamaBadResponse,
        ):
            if candidate.code == code:
                return candidate()
        return errors.OllamaBadResponse(detail=f"model failed with {code}")


__all__ = ["FAILURE_NODE", "MODEL_NODE", "ChatAgent"]
