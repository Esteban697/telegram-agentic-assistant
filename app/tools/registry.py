"""The tool registry: the single source of tool truth (PLAN.md §2.1, §5.3, §10 Phase 3).

Layer 4. It owns three things:

* the **registry** — every tool with its argument model, risk level and timeout, so "every tool
  has a level" is checkable instead of assumed (§4.3);
* the **invocation wrapper** — the `tool_invocations` ledger with the *ordered* precedence of
  §5.3, which is what makes a resumed node safe without suppressing a legitimate repeat;
* the bridge to the model — the JSON Schema the chat API receives, built from the same pydantic
  models the wrapper validates against, so the two can never disagree.

The ledger key is `(thread_id, run_key, attempt_no)` with

    run_key = f"{parent_checkpoint_id}:{node_name}:{tool_name}:{call_index}"

and every component is read from the checkpointed state or the run config — **never** from a
fresh model response. `tool_call_id` is recorded for observability only.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.errors import AssistantError
from app.llm.protocols import ToolSpec as ModelToolSpec
from app.policy.risk import RiskLevel, assert_registry_complete, requires_approval
from app.storage import repositories
from app.tools.base import ToolResult, validate_args

_logger = logging.getLogger(__name__)

STALE_INVOCATION_SECONDS_DEFAULT = 900
MAX_TOOL_ITERATIONS = 4


class ToolCallArgsMismatch(AssistantError):
    """Rule 1: the arguments under this key differ from the recorded ones.

    Checked **before** replay, and applied to every existing row including a `succeeded` one.
    A resumed node that re-words its call must fail loudly rather than receive the previous
    result or run again.
    """

    code = "tool_call_args_mismatch"
    message_key = "error.tool_args_changed"


class ToolCallReordered(AssistantError):
    """A reordered call list is an identity mismatch, not a wrong-result replay."""

    code = "tool_call_reordered"
    message_key = "error.tool_args_changed"


class ApprovalRequired(AssistantError):
    """A level-2 tool was invoked directly instead of through the approval path.

    PLAN.md §4.3: "level-2 tools have no direct execution path". Enforcing it here rather than in
    the graph's routing is what makes that a property of the *code* rather than of one `if` — the
    Phase 3 review executed a reservation through `invoke()` with no approval row at all.
    """

    code = "approval_required"
    message_key = "approval.in_progress"


class StaleToolInvocation(AssistantError):
    """Rule 3: a `started` row with no result, older than the lease (N15)."""

    code = "tool_invocation_unknown"
    message_key = "error.tool_unknown"


ToolHandler = Callable[..., Awaitable[ToolResult[Any]]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """**Frozen contract.** One tool: schema, policy and the callable that performs it.

    `handler` may be bound at registration time rather than at declaration, which is why it is
    optional here; the registry refuses to load a tool whose handler is missing.
    """

    name: str
    description: str
    args_model: type[BaseModel]
    risk_level: RiskLevel
    timeout_seconds: float = 30.0
    handler: ToolHandler | None = None

    @property
    def requires_approval(self) -> bool:
        return requires_approval(self.risk_level)

    def json_schema(self) -> dict[str, Any]:
        """The schema the model receives, derived from the arguments model it will be validated against."""

        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        return schema

    def as_model_tool(self) -> ModelToolSpec:
        return ModelToolSpec(
            name=self.name,
            description=self.description,
            parameters=self.json_schema(),
        )


def compose_run_key(
    *, parent_checkpoint_id: str | None, node_name: str, tool_name: str, call_index: int
) -> str:
    """The run-stable ledger key of §5.3.

    `parent_checkpoint_id` differs between turns, so two deliberate identical requests both
    execute; it is the same when a node is replayed, so an accidental re-execution replays.
    """

    return f"{parent_checkpoint_id or 'start'}:{node_name}:{tool_name}:{call_index}"


class ToolRegistry:
    """Holds the tools, validates their arguments, and wraps them with the ledger."""

    def __init__(
        self,
        tools: Sequence[ToolSpec],
        *,
        sessions: async_sessionmaker[AsyncSession],
        stale_seconds: int = STALE_INVOCATION_SECONDS_DEFAULT,
        allow_level3: bool = False,
    ) -> None:
        self._tools: dict[str, ToolSpec] = {}
        for spec in tools:
            if spec.handler is None:
                raise ValueError(f"tool {spec.name!r} has no handler bound")
            if spec.name in self._tools:
                raise ValueError(f"duplicate tool name {spec.name!r}")
            self._tools[spec.name] = spec

        # A tool without a level, or a level-3 tool, must be a *startup* failure (§4.3).
        assert_registry_complete(
            {name: spec.risk_level for name, spec in self._tools.items()},
            allow_level3=allow_level3,
        )
        self._sessions = sessions
        self._stale = timedelta(seconds=stale_seconds)

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise AssistantError(
                f"unknown tool {name!r}", context={"tool_name": name}
            ) from exc

    def names(self) -> list[str]:
        return sorted(self._tools)

    def level_of(self, name: str) -> RiskLevel:
        return self.get(name).risk_level

    def model_tools(self) -> list[ModelToolSpec]:
        """What the model is offered: every tool, including the level-2 ones.

        Level-2 tools are offered on purpose: the graph must be able to *prepare* an external
        action and ask for approval, which is the whole of spec §7.
        """

        return [self._tools[name].as_model_tool() for name in self.names()]

    def bind(self, name: str, handler: ToolHandler) -> "ToolRegistry":
        """Return a copy of this registry with one handler replaced.

        Used by tests to inject a slow or failing tool without touching the wiring.
        """

        bound = self.get(name)
        replacement = ToolSpec(
            name=bound.name,
            description=bound.description,
            args_model=bound.args_model,
            risk_level=bound.risk_level,
            timeout_seconds=bound.timeout_seconds,
            handler=handler,
        )
        clone = ToolRegistry(
            [replacement if spec.name == name else spec for spec in self._tools.values()],
            sessions=self._sessions,
            stale_seconds=int(self._stale.total_seconds()),
        )
        return clone

    # ------------------------------------------------------------------ #
    # Invocation with the ledger
    # ------------------------------------------------------------------ #
    async def invoke(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        thread_id: str,
        run_key: str,
        attempt_no: int,
        user_id: int,
        context: dict[str, Any] | None = None,
    ) -> ToolResult[Any]:
        """Run a tool through the ledger, with the precedence of §5.3.

        Read-only tools skip the ledger: repeating a read is harmless and the write would be
        pure overhead on the hot path.
        """

        spec = self.get(tool_name)
        if spec.requires_approval:
            # Approval consumption is the only entry point for these.
            raise ApprovalRequired(
                f"{tool_name} requires an approval; invoke it through the approval path",
                context={"tool_name": tool_name},
            )
        args = validate_args(spec.args_model, arguments)
        args_hash = _hash_args(args)

        if spec.risk_level is RiskLevel.READ:
            return await self._call(spec, args, user_id=user_id, context=context)

        async with self._sessions() as session:
            existing = await repositories.find_invocation(
                session, thread_id=thread_id, run_key=run_key, attempt_no=attempt_no
            )

        if existing is not None:
            # Rule 1 FIRST, and it outranks replay: a mismatching hash on *any* row — including
            # a `succeeded` one — is a hard error, never a silent wrong-result replay.
            if existing.args_hash != args_hash:
                raise ToolCallArgsMismatch(
                    f"{tool_name} under {run_key} has different arguments than the recorded call",
                    context={"tool_name": tool_name, "run_key": run_key},
                )
            if existing.state == "succeeded":
                _logger.info(
                    "tool invocation replayed from the ledger",
                    extra={"tool_name": tool_name, "run_key": run_key, "status": "replayed"},
                )
                return ToolResult.success(
                    existing.result_json or {}, replayed=True, rule="replay"
                )
            if existing.state == "started":
                started = existing.started_at
                if started.tzinfo is None:
                    started = started.replace(tzinfo=UTC)
                if datetime.now(UTC) - started > self._stale:
                    async with self._sessions() as session, session.begin():
                        await repositories.finish_invocation(
                            session,
                            thread_id=thread_id,
                            run_key=run_key,
                            attempt_no=attempt_no,
                            state="unknown",
                            error="stale started row",
                        )
                    raise StaleToolInvocation(
                        f"{tool_name} may or may not have run; the ledger row is stale",
                        context={"tool_name": tool_name, "run_key": run_key},
                    )
                # A fresh `started` row means the effect is in flight right now.
                raise StaleToolInvocation(
                    f"{tool_name} is already in flight for this call",
                    context={"tool_name": tool_name, "run_key": run_key},
                )
            if existing.state in {"failed", "unknown"}:
                return ToolResult.failure(
                    f"tool_{existing.state}",
                    message_key="error.tool_unknown",
                    detail=f"{tool_name} did not complete ({existing.state})",
                )

        # No row: claim the call by writing `started` and committing *before* the effect.
        async with self._sessions() as session, session.begin():
            inserted = await repositories.start_invocation(
                session,
                thread_id=thread_id,
                run_key=run_key,
                attempt_no=attempt_no,
                tool_name=tool_name,
                tool_call_id=str((context or {}).get("tool_call_id") or "") or None,
                args_hash=args_hash,
            )
        if inserted == 0:  # pragma: no cover - a concurrent writer took it first
            raise StaleToolInvocation(
                f"{tool_name} was claimed concurrently", context={"run_key": run_key}
            )

        started_at = time.perf_counter()
        try:
            result = await self._call(spec, args, user_id=user_id, context=context)
        except AssistantError as exc:
            async with self._sessions() as session, session.begin():
                await repositories.finish_invocation(
                    session,
                    thread_id=thread_id,
                    run_key=run_key,
                    attempt_no=attempt_no,
                    state="failed",
                    error=exc.code,
                )
            raise
        except Exception as exc:  # pragma: no cover - defensive
            async with self._sessions() as session, session.begin():
                await repositories.finish_invocation(
                    session,
                    thread_id=thread_id,
                    run_key=run_key,
                    attempt_no=attempt_no,
                    state="failed",
                    error=type(exc).__name__,
                )
            raise

        async with self._sessions() as session, session.begin():
            await repositories.finish_invocation(
                session,
                thread_id=thread_id,
                run_key=run_key,
                attempt_no=attempt_no,
                state="succeeded",
                result_json=result.data if isinstance(result.data, dict) else {"value": result.data},
            )
        _logger.info(
            "tool invocation completed",
            extra={
                "tool_name": tool_name,
                "run_key": run_key,
                "duration_ms": round((time.perf_counter() - started_at) * 1000, 1),
                "status": "ok",
            },
        )
        return result

    async def execute_approved(self, *, execution: Any, user_id: int) -> ToolResult[Any]:
        """Run the tool named by a **claimed** approval.

        It takes the claim's `Execution` rather than a name and a dict: a bare `(name, args)` pair
        carries no evidence that anything was approved, so the previous signature let any caller
        execute a level-2 tool. The claim is the proof, and its identity is recorded with the
        result.

        The approval row *is* the ledger for this call, so the tool ledger is not written again:
        a second ledger would need a run_key the callback does not have, and would imply a replay
        path the approval already provides.
        """

        if not getattr(execution, "claimed", False):
            raise ApprovalRequired(
                "execute_approved requires a claim that actually succeeded",
                context={"action_id": getattr(execution, "action_id", None)},
            )
        tool_name = str(execution.action_type)
        spec = self.get(tool_name)
        if not spec.requires_approval:
            # A level-0/1 tool must not be routed through the approval path either: it would
            # bypass the tool ledger this registry exists to maintain.
            raise ApprovalRequired(
                f"{tool_name} does not require approval and must be invoked directly",
                context={"tool_name": tool_name},
            )
        args = validate_args(spec.args_model, dict(execution.args or {}))
        return await self._call(
            spec,
            args,
            user_id=user_id,
            context={"approved": True, "action_id": getattr(execution, "action_id", None)},
        )

    async def _call(
        self,
        spec: ToolSpec,
        args: BaseModel,
        *,
        user_id: int,
        context: dict[str, Any] | None,
    ) -> ToolResult[Any]:
        assert spec.handler is not None
        return await spec.handler(user_id=user_id, args=args, context=context or {})


def _hash_args(args: BaseModel) -> str:
    from app.policy.approval import compute_args_hash

    return compute_args_hash(args.model_dump(mode="json"))


__all__ = [
    "ApprovalRequired",
    "MAX_TOOL_ITERATIONS",
    "STALE_INVOCATION_SECONDS_DEFAULT",
    "StaleToolInvocation",
    "ToolCallArgsMismatch",
    "ToolCallReordered",
    "ToolRegistry",
    "ToolSpec",
    "compose_run_key",
]
