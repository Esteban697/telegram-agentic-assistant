"""The Phase 3 graph: answer / tool-call / approval / clarify / hand-off (PLAN.md §10 Phase 3).

Nodes:

* `call_model` — the model, with the registry's tools bound natively (A10 proved this model's
  tool calling works; `format=<JSON Schema>` is deliberately **not** used anywhere, because it
  was measured not to constrain this model);
* `run_tools` — level 0/1 tools run through the ledger; a level-2 call **stops** the turn and
  becomes a pending approval, and nothing executes;
* `await_approval` — terminal marker: the handler sends the prompt;
* `report_failure` — terminal marker for a controlled failure.

Three properties the tests lean on:

* the **run key** is composed from checkpointed state (`turn`), so a replayed node replays and a
  new turn is a new key;
* a level-2 call can never execute here — there is no code path from this graph to an external
  effect, which is what makes acceptance (a) a property of the architecture and not of a branch;
* the tool loop is capped, so a model that keeps calling tools ends the turn instead of spinning.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.agent.policies import ToolNotAllowed, decide, prepare_approval
from app.agent.prompts import build_tool_hint
from app.agent.routing import (
    ANSWER,
    APPROVAL,
    FAILURE,
    TOOLS,
    route_after_model,
    route_after_tools,
)
from app.agent.state import ChatState, from_message_dicts, to_message_dicts
from app.errors import AssistantError, BudgetExceeded, OllamaBadResponse
from app.llm.protocols import ChatMessage, ChatModel, ChatResult
from app.tools.base import validate_args
from app.tools.registry import (
    MAX_TOOL_ITERATIONS,
    ToolRegistry,
    compose_run_key,
)

_logger = logging.getLogger(__name__)

MODEL_NODE = "call_model"
TOOLS_NODE = "run_tools"
APPROVAL_NODE = "await_approval"
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
        registry: ToolRegistry | None = None,
        approvals: Any | None = None,
        max_iterations: int = MAX_TOOL_ITERATIONS,
        interrupt_before: Sequence[str] | None = None,
        interrupt_after: Sequence[str] | None = None,
    ) -> None:
        self._model = model
        self._system_prompt = system_prompt
        self._budget_seconds = budget_seconds
        self._registry = registry
        self._approvals = approvals
        self._max_iterations = max_iterations
        self._checkpointer = checkpointer if checkpointer is not None else InMemorySaver()
        self._interrupt_before = list(interrupt_before or [])
        self._interrupt_after = list(interrupt_after or [])
        self._graph = self._build_graph()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    async def run(
        self,
        messages: Sequence[ChatMessage],
        *,
        thread_id: str | None = None,
        user_id: int | None = None,
        chat_id: int | None = None,
    ) -> ChatResult:
        """Run a turn over the already-shaped prompt input."""

        shaped = self._shape(messages)
        # `turn` is an explicit counter held in the checkpointed state, incremented once per turn.
        # It used to be derived from the message count, which was monotonic only because nothing
        # removes messages from the state: any future in-graph truncation would have repeated a
        # `turn` and silently replayed another turn's tool result.
        previous = await self._turn_of(thread_id or "default")
        initial: ChatState = {
            "messages": shaped,
            "turn": previous + 1,
            "iterations": 0,
            "tool_calls": [],
            "tool_results": [],
            "pending_approval": None,
            "thread_id": thread_id or "default",
            "user_id": int(user_id or 0),
            "chat_id": int(chat_id or 0),
        }
        return await self._invoke(initial, thread_id=thread_id)

    async def resume(self, thread_id: str) -> ChatResult:
        """Continue a thread from its last checkpoint."""

        return await self._invoke(None, thread_id=thread_id)

    async def _invoke(self, initial: Any, *, thread_id: str | None) -> ChatResult:
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

    async def pending_approval(self, thread_id: str) -> dict[str, Any] | None:
        snapshot = await self._graph.aget_state(self._config(thread_id))
        return (snapshot.values or {}).get("pending_approval")

    async def _turn_of(self, thread_id: str) -> int:
        """The turn counter as the checkpoint has it, or 0 for a fresh thread."""

        snapshot = await self._graph.aget_state(self._config(thread_id))
        return int((snapshot.values or {}).get("turn") or 0)

    async def turn_of(self, thread_id: str) -> int:
        """Public view of the per-thread turn counter (asserted by the tests)."""

        return await self._turn_of(thread_id)

    async def history(self, thread_id: str) -> list[ChatMessage]:
        if not thread_id:
            return []
        snapshot = await self._graph.aget_state(self._config(thread_id))
        raw = (snapshot.values or {}).get("messages") or []
        return [message for message in from_message_dicts(raw) if message.role != "system"]

    async def is_interrupted(self, thread_id: str) -> bool:
        snapshot = await self._graph.aget_state(self._config(thread_id))
        return bool(snapshot.next)

    async def thread_exists(self, thread_id: str) -> bool:
        if not thread_id:
            return False
        snapshot = await self._graph.aget_state(self._config(thread_id))
        return bool(snapshot.values)

    @property
    def model_name(self) -> str:
        return self._model.model

    @property
    def registry(self) -> ToolRegistry | None:
        return self._registry

    # ------------------------------------------------------------------ #
    # Graph
    # ------------------------------------------------------------------ #
    def _build_graph(self):  # noqa: ANN202 - compiled graph has no public type
        builder = StateGraph(ChatState)
        builder.add_node(MODEL_NODE, self._call_model)
        builder.add_node(TOOLS_NODE, self._run_tools)
        builder.add_node(APPROVAL_NODE, self._await_approval)
        builder.add_node(FAILURE_NODE, self._report_failure)
        builder.add_edge(START, MODEL_NODE)
        builder.add_conditional_edges(
            MODEL_NODE,
            route_after_model,
            {TOOLS: TOOLS_NODE, ANSWER: END, FAILURE: FAILURE_NODE},
        )
        builder.add_conditional_edges(
            TOOLS_NODE,
            lambda state: route_after_tools(state, max_iterations=self._max_iterations),
            {TOOLS: MODEL_NODE, APPROVAL: APPROVAL_NODE, ANSWER: END},
        )
        builder.add_edge(APPROVAL_NODE, END)
        builder.add_edge(FAILURE_NODE, END)
        return builder.compile(
            checkpointer=self._checkpointer,
            interrupt_before=self._interrupt_before or None,
            interrupt_after=self._interrupt_after or None,
        )

    @staticmethod
    def _config(thread_id: str | None) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id or "default"}}

    def _shape(self, messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
        shaped: list[dict[str, Any]] = [{"role": "system", "content": self._system_prompt}]
        if self._registry is not None:
            shaped.append(
                {
                    "role": "system",
                    "content": build_tool_hint(", ".join(self._registry.names())),
                }
            )
        shaped.extend(to_message_dicts(list(messages)))
        return shaped

    async def _call_model(self, state: ChatState) -> ChatState:
        messages = from_message_dicts(state.get("messages", []))
        conversation = [message for message in messages if message.role != "system"]

        try:
            result = await self._model.chat(
                messages,
                tools=self._registry.model_tools() if self._registry else None,
            )
        except AssistantError as exc:
            _logger.warning("model call failed", extra=exc.as_log_fields())
            return {"error_code": exc.code}
        except Exception as exc:  # pragma: no cover - unexpected provider bug
            _logger.error(
                "unexpected model failure",
                extra={"error_code": "unexpected_model_error"},
                exc_info=exc,
            )
            return {"error_code": OllamaBadResponse().code}

        tool_calls: list[dict[str, Any]] = [
            {
                "tool_name": call.name,
                "arguments": dict(call.arguments),
                "call_id": call.call_id or "",
                "call_index": index,
            }
            for index, call in enumerate(result.tool_calls)
        ]
        _logger.info(
            "model turn complete",
            extra={
                "model": result.model,
                "tool_calls": len(tool_calls),
                "duration_ms": round(result.latency_ms or 0.0, 1),
                "status": "ok",
            },
        )
        if tool_calls:
            # The assistant turn that *requested* the tools goes into the transcript so the
            # model sees its own request on the follow-up round.
            return {
                "messages": [
                    *state.get("messages", []),
                    {
                        "role": "assistant",
                        "content": result.content or "",
                        "tool_calls": tool_calls,
                    },
                ],
                "tool_calls": tool_calls,
                "model": result.model,
                "latency_ms": result.latency_ms,
                "error_code": None,
            }

        accumulated = [
            *to_message_dicts(conversation),
            {"role": "assistant", "content": result.content},
        ]
        return {
            "messages": accumulated,
            "tool_calls": [],
            "reply": result.content,
            "model": result.model,
            "latency_ms": result.latency_ms,
            "error_code": None,
        }

    async def _run_tools(self, state: ChatState) -> ChatState:
        """Run the level 0/1 calls; a level-2 call becomes a pending approval and stops."""

        assert self._registry is not None, "the graph has no tool registry"
        thread_id = str(state.get("thread_id") or "default")
        turn = int(state.get("turn") or 1)
        user_id = int(state.get("user_id") or 0)
        chat_id = int(state.get("chat_id") or 0)
        messages = list(state.get("messages", []))
        results = list(state.get("tool_results", []))
        pending: dict[str, Any] | None = None

        for call in state.get("tool_calls", []):
            tool_name = str(call.get("tool_name", ""))
            arguments = dict(call.get("arguments", {}))
            call_index = int(call.get("call_index", 0))

            try:
                decision = decide(self._registry, tool_name)
            except ToolNotAllowed as exc:
                _logger.warning("tool refused by policy", extra=exc.as_log_fields())
                results.append({"tool_name": tool_name, "ok": False, "error_code": exc.code})
                messages.append(
                    {"role": "tool", "content": f"{tool_name}: {exc.code}"}
                )
                continue

            if decision.needs_approval:
                if self._approvals is None:  # pragma: no cover - always wired in production
                    results.append(
                        {"tool_name": tool_name, "ok": False, "error_code": "no_approvals"}
                    )
                    messages.append({"role": "tool", "content": f"{tool_name}: no_approvals"})
                    continue
                # Validate before preparing: spec §10 requires an argument schema for *every*
                # tool, and an unvalidated payload showed the user an approval prompt for a call
                # that would be rejected at execution.
                spec = self._registry.get(tool_name)
                request = await prepare_approval(
                    self._approvals,
                    user_id=user_id,
                    chat_id=chat_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    validator=lambda raw, model=spec.args_model: validate_args(
                        model, raw
                    ).model_dump(mode="json"),
                )
                pending = {
                    "action_id": request.action_id,
                    "action_type": request.action_type,
                    "args_hash": request.args_hash,
                    "args_revision": request.args_revision,
                    "preview_text": request.preview_text,
                    "expires_at": request.expires_at.isoformat(),
                    "call_index": call_index,
                }
                _logger.info(
                    "approval prepared",
                    extra={
                        "action_id": request.action_id,
                        "tool_name": tool_name,
                        "status": "pending",
                    },
                )
                # One approval at a time: a second level-2 call in the same turn would need a
                # second prompt, and stacking prompts is what the live-action index prevents.
                break

            run_key = compose_run_key(
                parent_checkpoint_id=f"turn{turn}",
                node_name=TOOLS_NODE,
                tool_name=tool_name,
                call_index=call_index,
            )
            try:
                outcome = await self._registry.invoke(
                    tool_name=tool_name,
                    arguments=arguments,
                    thread_id=thread_id,
                    run_key=run_key,
                    attempt_no=1,
                    user_id=user_id,
                    context={"tool_call_id": call.get("call_id")},
                )
                results.append(
                    {
                        "tool_name": tool_name,
                        "ok": outcome.ok,
                        "run_key": run_key,
                        "error_code": outcome.error_code,
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "content": _render_tool_result(tool_name, outcome),
                    }
                )
            except AssistantError as exc:
                # Rule 1 of §5.3 is a loud failure, and the turn says so instead of guessing.
                _logger.warning("tool invocation refused", extra=exc.as_log_fields())
                results.append({"tool_name": tool_name, "ok": False, "error_code": exc.code})
                messages.append({"role": "tool", "content": f"{tool_name}: {exc.code}"})

        return {
            "messages": messages,
            "tool_calls": [],
            "tool_results": results,
            "pending_approval": pending,
            "iterations": int(state.get("iterations") or 0) + 1,
        }

    @staticmethod
    def _await_approval(state: ChatState) -> ChatState:
        """Terminal marker: the handler renders the prompt from `pending_approval`."""

        return {"pending_approval": state.get("pending_approval")}

    @staticmethod
    def _report_failure(state: ChatState) -> ChatState:
        return {"error_code": state.get("error_code")}

    @staticmethod
    def _error_for(code: str) -> AssistantError:
        from app import errors

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


def _render_tool_result(tool_name: str, outcome: Any) -> str:
    """Turn a structured result into the text the model sees for that tool call."""

    if not outcome.ok:
        return f"{tool_name}: error {outcome.error_code}"
    return f"{tool_name}: {json.dumps(outcome.data, ensure_ascii=False, default=str)}"


__all__ = ["APPROVAL_NODE", "FAILURE_NODE", "MODEL_NODE", "TOOLS_NODE", "ChatAgent"]
