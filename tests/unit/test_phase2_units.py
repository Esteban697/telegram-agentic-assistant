"""Phase 2 units that need no database: tool schemas, idempotency keys, retry schedule,
conversation-service shaping with a fake store, and the frozen repository signatures."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.errors import AssistantError
from app.llm.protocols import ChatMessage
from app.services.conversation import ConversationService
from app.services.reminders import retry_delay_seconds
from app.policy.risk import RiskLevel
from app.tools.base import ToolResult, validate_args
from app.tools.registry import ToolSpec
from app.tools.memory import MemoryTools
from app.tools.memory import specs as memory_specs
from app.tools.memory import SavePreferenceArgs, SearchMemoryArgs
from app.tools.tasks import TaskTools
from app.tools.tasks import specs as task_specs
from app.tools.tasks import CreateReminderArgs, creation_key

REPO = Path(__file__).resolve().parents[2]
REPOSITORIES = REPO / "app" / "storage" / "repositories.py"


# --------------------------------------------------------------------------- #
# Tool schemas (spec §10)
# --------------------------------------------------------------------------- #
def test_every_tool_has_a_schema_a_risk_level_and_a_description() -> None:
    specs = (*memory_specs(MemoryTools(sessions=None)), *task_specs(TaskTools(sessions=None)))
    assert specs, "no tool specs found"
    for spec in specs:
        assert isinstance(spec, ToolSpec)
        assert spec.name and spec.description
        assert issubclass(spec.args_model, __import__("pydantic").BaseModel)
        assert isinstance(spec.risk_level, RiskLevel)


def test_the_read_and_write_levels_are_what_the_spec_says() -> None:
    by_name = {
        spec.name: spec.risk_level
        for spec in (*memory_specs(MemoryTools(sessions=None)), *task_specs(TaskTools(sessions=None)))
    }
    assert by_name["search_personal_memory"] == 0
    assert by_name["list_tasks"] == 0
    assert by_name["save_preference"] == 1
    assert by_name["create_reminder"] == 1
    assert by_name["complete_task"] == 1
    assert by_name["cancel_reminder"] == 1
    assert by_name["delete_memory"] == 1
    assert 2 not in by_name.values(), "level 2 belongs to Phase 3's approvals"


@pytest.mark.parametrize(
    "raw",
    [
        {},  # missing everything
        {"key": "ciudad"},  # missing value
        {"key": "", "value": "Madrid"},  # empty key
        {"key": "x" * 200, "value": "Madrid"},  # key too long
        {"key": "ciudad", "value": "x" * 5000},  # value too long
    ],
)
def test_invalid_arguments_are_rejected_with_a_typed_error(raw: dict[str, Any]) -> None:
    with pytest.raises(AssistantError) as excinfo:
        validate_args(SavePreferenceArgs, raw)
    assert excinfo.value.code == "internal_error"
    assert "invalid arguments" in excinfo.value.detail


def test_valid_arguments_pass() -> None:
    args = validate_args(SearchMemoryArgs, {"query": "ciudad"})
    assert args.query == "ciudad"


# --------------------------------------------------------------------------- #
# Reminder idempotency key
# --------------------------------------------------------------------------- #
def test_the_creation_key_is_stable_and_content_addressed() -> None:
    moment = datetime(2026, 11, 3, 8, 30, tzinfo=UTC)
    first = creation_key(user_id=42, title="Llamar al dentista", due_at_utc=moment)
    second = creation_key(user_id=42, title="  llamar AL DENTISTA ", due_at_utc=moment)
    assert first == second, "case and whitespace must not create a second reminder"

    other_user = creation_key(user_id=43, title="Llamar al dentista", due_at_utc=moment)
    other_time = creation_key(
        user_id=42, title="Llamar al dentista", due_at_utc=moment.replace(hour=9)
    )
    assert len({first, other_user, other_time}) == 3


def test_the_creation_key_is_a_bounded_string() -> None:
    key = creation_key(user_id=1, title="x", due_at_utc=datetime.now(UTC))
    assert key.startswith("v1:")
    assert len(key) <= 64


# --------------------------------------------------------------------------- #
# Dispatcher backoff
# --------------------------------------------------------------------------- #
def test_retry_backoff_is_bounded_and_monotonic() -> None:
    delays = [retry_delay_seconds(attempt) for attempt in range(1, 12)]
    assert delays == sorted(delays)
    assert delays[0] == 60
    assert max(delays) == 3600, "an unbounded backoff would starve the reminder"


# --------------------------------------------------------------------------- #
# The conversation service shapes the prompt
# --------------------------------------------------------------------------- #
class FakeHistory:
    def __init__(self, history: list[ChatMessage]) -> None:
        self._history = history
        self.asked: list[str] = []

    async def history(self, thread_id: str) -> list[ChatMessage]:
        self.asked.append(thread_id)
        return list(self._history)


class FakeStore:
    def __init__(self, history: list[ChatMessage]) -> None:
        self.history_provider = FakeHistory(history)
        self.recorded: list[tuple[int, str, str]] = []
        self.resets = 0

    async def thread_id(self, *, user_id: int, chat_id: int) -> str:
        return "thread-1"

    async def history(self, *, chat_id: int) -> list[ChatMessage]:
        return await self.history_provider.history("thread-1")

    async def reset(self, *, chat_id: int) -> str:
        self.resets += 1
        return "thread-2"

    async def record_turn(self, **kwargs: Any) -> None:
        self.recorded.append((kwargs["chat_id"], kwargs["user_text"], kwargs["assistant_text"]))


async def test_prepare_shapes_history_plus_the_new_message() -> None:
    store = FakeStore([ChatMessage(role="user", content="antes")])
    service = ConversationService(store, max_messages=10)

    prepared = await service.prepare(user_id=42, chat_id=4242, text="ahora")

    assert prepared.thread_id == "thread-1"
    assert [(m.role, m.content) for m in prepared.prompt_input] == [
        ("user", "antes"),
        ("user", "ahora"),
    ]
    assert prepared.history_length == 1


async def test_prepare_trims_history_to_the_cap() -> None:
    history = [ChatMessage(role="user", content=f"m{i}") for i in range(50)]
    service = ConversationService(FakeStore(history), max_messages=5)

    prepared = await service.prepare(user_id=42, chat_id=4242, text="nuevo")

    assert prepared.history_length == 5
    assert [m.content for m in prepared.prompt_input[:5]] == ["m45", "m46", "m47", "m48", "m49"]
    assert prepared.prompt_input[-1].content == "nuevo"


async def test_record_and_reset_are_delegated() -> None:
    store = FakeStore([])
    service = ConversationService(store, max_messages=10)

    await service.record(chat_id=4242, user_text="hola", assistant_text="buenas")
    assert store.recorded == [(4242, "hola", "buenas")]

    assert await service.reset(chat_id=4242) == "thread-2"
    assert store.resets == 1


# --------------------------------------------------------------------------- #
# Frozen repository signatures
# --------------------------------------------------------------------------- #
def _signature_of(name: str) -> dict[str, str | None]:
    tree = ast.parse(REPOSITORIES.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            arguments = node.args
            positional = [a.arg for a in arguments.args]
            keyword_only = [a.arg for a in arguments.kwonlyargs]
            names = [*positional, *keyword_only]
            # Keyword-only defaults are positionally aligned with `kwonlyargs`; the leading
            # Nones belong to the required positional parameters.
            defaults: list[str | None] = [None] * (len(positional) - len(arguments.defaults))
            defaults += [ast.unparse(d) for d in arguments.defaults]
            defaults += [
                ast.unparse(d) if d is not None else None for d in arguments.kw_defaults
            ]
            return dict(zip(names, defaults, strict=False))
    raise AssertionError(f"{name} not found in repositories.py")


def test_search_chunks_signature_is_frozen() -> None:
    """Phase 3 and Phase 4 build on this shape; changing it is a contract change."""

    assert _signature_of("search_chunks") == {
        "session": None,
        "owner_user_id": None,
        "query_embedding": None,
        "embed_model": None,
        "top_k": None,
        "document_ids": "None",
    }


def test_the_dispatcher_repository_signatures_are_frozen() -> None:
    assert _signature_of("claim_due_tasks") == {
        "session": None,
        "batch": None,
        "executor": None,
    }
    assert _signature_of("claim_dispatch") == {
        "session": None,
        "task_id": None,
        "occurrence_at": None,
        "dispatch_key": None,
        "executor": None,
    }
    assert _signature_of("reconcile_stale_dispatches") == {
        "session": None,
        "lease_seconds": None,
        "executor": None,
    }


def test_the_claim_predicate_enforces_the_attempt_cap_in_sql() -> None:
    """B1: the cap must be in the statement, not in the caller's head."""

    source = REPOSITORIES.read_text(encoding="utf-8")
    assert "Task.attempts < Task.max_attempts" in source
    assert "skip_locked=True" in source, "concurrent dispatchers need SKIP LOCKED"


# The repository-wide chokepoint and SQL guards live in tests/unit/test_contract_guards.py,
# because they must walk every module under app/ rather than only repositories.py (item 6).


def test_tool_result_is_structured_not_textual() -> None:
    ok = ToolResult.success({"a": 1}, count=1)
    failed = ToolResult.failure("nope", message_key="error.generic")
    assert ok.ok and ok.data == {"a": 1} and ok.meta["count"] == 1
    assert not failed.ok and failed.error_code == "nope" and failed.message_key == "error.generic"
