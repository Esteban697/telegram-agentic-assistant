"""The conversation graph (PLAN.md §10 Phase 1 deliverable 9, Phase 2 persistence).

LangGraph from Phase 1 on purpose, so Phase 2 only has to **attach the checkpointer** rather
than restructure. Two things changed in Phase 2:

* the graph is compiled with a checkpointer, so a thread's state survives the process;
* the node returns the accumulated message list, so `aget_state` is the single source of
  conversational history (decision 3). `messages` — the audit table — is never read as history.

The graph depends on the :class:`~app.llm.protocols.ChatModel` **protocol** and receives a
concrete instance by constructor injection from ``main.py``: nothing under ``agent/`` imports
HTTP provider code or ``app.storage``, and ``history()`` reads graph state, not a store.

``REQUEST_BUDGET_SECONDS`` is applied as a hard timeout around the graph call (NN8). Provider
failures are caught inside the node and turned into a state field, so a provider exception
cannot escape the graph and skip whatever bookkeeping the caller needs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.agent.routing import DONE, FAILED, route_after_model
from app.agent.state import ChatState, from_message_dicts, to_message_dicts
from app.errors import AssistantError, BudgetExceeded, OllamaBadResponse
from app.llm.protocols import ChatMessage, ChatModel, ChatResult

_logger = logging.getLogger(__name__)

MODEL_NODE = "call_model"
FAILURE_NODE = "report_failure"


class ChatAgent:
    """Runs one conversational turn through the graph, per thread."""

    def __init__(
        self,
        model: ChatModel,
        *,
        system_prompt: str,
        budget_seconds: float,
        checkpointer: Any | None = None,
        interrupt_before: Sequence[str] | None = None,
        interrupt_after: Sequence[str] | None = None,
    ) -> None:
        self._model = model
        self._system_prompt = system_prompt
        self._budget_seconds = budget_seconds
        # Phase 3 interrupts before a tool node to ask for approval; Phase 2 uses
        # `interrupt_after` to prove that a run stopped *after a completed step* continues in a
        # new process (interrupting before the first node would prove much less).
        self._interrupt_before = list(interrupt_before or [])
        self._interrupt_after = list(interrupt_after or [])
        # Without an injected checkpointer the graph still keeps state, in memory. That keeps
        # one code path for development, tests and production: only the *saver* differs.
        self._checkpointer = checkpointer if checkpointer is not None else InMemorySaver()
        self._durable = checkpointer is not None
        self._graph = self._build_graph()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    async def run(
        self, messages: Sequence[ChatMessage], *, thread_id: str | None = None
    ) -> ChatResult:
        """Run a turn over the already-shaped prompt input.

        ``messages`` is supplied by the caller, never read from a store here. When ``thread_id``
        is given, the run is bound to that thread so the checkpointer persists the state.
        """

        shaped = self._shape(messages)
        initial: ChatState = {"messages": shaped}
        config = self._config(thread_id)

        try:
            async with asyncio.timeout(self._budget_seconds):
                final_state = await self._graph.ainvoke(initial, config=config)
        except TimeoutError as exc:
            raise BudgetExceeded(
                f"turn exceeded REQUEST_BUDGET_SECONDS={self._budget_seconds}",
                context={"budget_seconds": self._budget_seconds},
            ) from exc

        error_code = final_state.get("error_code")
        if error_code:
            raise self._error_for(error_code)

        if await self.is_interrupted(thread_id):
            # The run stopped at a boundary, so this turn produced no answer. Returning the
            # `reply` still sitting in the state would hand the caller the *previous* turn's
            # answer as if it were this one's — which is exactly what an approval interrupt in
            # Phase 3 must not do.
            _logger.info(
                "run stopped at an interrupt boundary",
                extra={"thread_id": thread_id, "status": "interrupted"},
            )
            return ChatResult(content="", model=self._model.model, latency_ms=None)

        return ChatResult(
            content=str(final_state.get("reply", "")),
            model=str(final_state.get("model") or self._model.model),
            latency_ms=final_state.get("latency_ms"),
        )

    async def history(self, thread_id: str) -> list[ChatMessage]:
        """Return the retained conversational history for a thread, oldest first.

        This is the `HistoryProvider` seam: ``ConversationStore`` calls it, and because it is a
        structural protocol the storage layer never imports this module (PLAN.md §2.2).
        """

        if not thread_id:
            return []
        snapshot = await self._graph.aget_state(self._config(thread_id))
        raw = (snapshot.values or {}).get("messages") or []
        return [message for message in from_message_dicts(raw) if message.role != "system"]

    async def is_interrupted(self, thread_id: str) -> bool:
        """True when the thread stopped before a node and is waiting to be resumed."""

        snapshot = await self._graph.aget_state(self._config(thread_id))
        return bool(snapshot.next)

    async def resume(self, thread_id: str) -> ChatResult:
        """Continue a thread from its last checkpoint.

        Invoking with ``None`` input tells LangGraph to pick up where the checkpoint left off
        rather than starting a new turn.
        """

        try:
            async with asyncio.timeout(self._budget_seconds):
                final_state = await self._graph.ainvoke(None, config=self._config(thread_id))
        except TimeoutError as exc:
            raise BudgetExceeded(
                f"resume exceeded REQUEST_BUDGET_SECONDS={self._budget_seconds}",
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

    async def thread_exists(self, thread_id: str) -> bool:
        """True when the thread has at least one checkpoint."""

        if not thread_id:
            return False
        snapshot = await self._graph.aget_state(self._config(thread_id))
        return bool(snapshot.values)

    @property
    def model_name(self) -> str:
        """The configured model name, straight from the injected port."""

        return self._model.model

    @property
    def is_durable(self) -> bool:
        """True when the graph is backed by a real (non-memory) checkpointer."""

        return self._durable

    # ------------------------------------------------------------------ #
    # Graph
    # ------------------------------------------------------------------ #
    def _build_graph(self):  # noqa: ANN202 - compiled graph has no public type
        builder = StateGraph(ChatState)
        builder.add_node(MODEL_NODE, self._call_model)
        builder.add_node(FAILURE_NODE, self._report_failure)
        builder.add_edge(START, MODEL_NODE)
        builder.add_conditional_edges(
            MODEL_NODE, route_after_model, {DONE: END, FAILED: FAILURE_NODE}
        )
        builder.add_edge(FAILURE_NODE, END)
        return builder.compile(
            checkpointer=self._checkpointer,
            interrupt_before=self._interrupt_before or None,
            interrupt_after=self._interrupt_after or None,
        )

    @staticmethod
    def _config(thread_id: str | None) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id or "default"}}

    def _shape(self, messages: Sequence[ChatMessage]) -> list[dict[str, str]]:
        # Uses the shared conversion helper rather than a second hand-rolled loop, so the
        # dict shape the graph carries is defined in exactly one place (app/agent/state.py).
        return [{"role": "system", "content": self._system_prompt}, *to_message_dicts(list(messages))]

    async def _call_model(self, state: ChatState) -> ChatState:
        messages = from_message_dicts(state.get("messages", []))
        conversation = [message for message in messages if message.role != "system"]

        try:
            result = await self._model.chat(messages)
        except AssistantError as exc:
            # Deliberately not re-raised: the graph records the failure and the agent maps it
            # back to a typed error, so the checkpointer still sees a terminal state.
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
        # The node returns the *accumulated* list, so the checkpoint holds the whole
        # conversation and `history()` has something to read (the MVP has no reducer).
        accumulated = [
            *to_message_dicts(conversation),
            {"role": "assistant", "content": result.content},
        ]
        return {
            "messages": accumulated,
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
