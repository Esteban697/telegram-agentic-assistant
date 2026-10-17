"""Phase 3 acceptance (a)-(h): approvals, the ledger, the watchdog (PLAN.md §10 Phase 3).

Every test asserts its own **precondition** as well as its outcome, so it cannot silently stop
exercising the contended or declined path it exists for. The stub model is scripted to request
tools, which is what makes conversational tool selection testable without a provider.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from app.agent.graph import ChatAgent
from app.agent.prompts import build_system_prompt
from app.bot import messages
from app.bot.handlers import TelegramHandlers
from app.config import Settings
from app.errors import AssistantError
from app.llm.protocols import ChatResult, ToolCall
from app.policy.approval import ZERO_ROW_KEYS, ActionState
from app.policy.authz import TelegramIdentity
from app.services.approvals import ApprovalService
from app.services.conversation import ConversationService
from app.services.work_queue import WorkQueue
from app.storage import repositories
from app.storage.checkpointing import build_checkpointer
from app.storage.postgres import build_database
from app.storage.stores import build_stores
from app.tools.memory import MemoryTools
from app.tools.memory import specs as memory_specs
from app.tools.registry import ToolCallArgsMismatch, ToolRegistry, compose_run_key
from app.tools.restaurants import RestaurantTools
from app.tools.restaurants import specs as restaurant_specs
from app.tools.tasks import TaskTools
from app.tools.tasks import specs as task_specs
from tests.conftest import (
    CHAT_ID,
    TEST_DATABASE_URL,
    USER_ID,
    RecordingTelegram,
    make_db_settings,
    truncate_all,
)

REPO = Path(__file__).resolve().parents[2]
OTHER_USER = 77

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set"),
]


# --------------------------------------------------------------------------- #
# A scripted model
# --------------------------------------------------------------------------- #
class ScriptedModel:
    """Returns the next scripted reply; a tool call when the script says so."""

    model = "scripted-model"

    def __init__(self, script: list[ChatResult], *, delay: float = 0.0) -> None:
        self._script = script
        self._index = 0
        self.calls: list[dict[str, Any]] = []
        self._delay = delay

    async def chat(self, messages, **kwargs: Any) -> ChatResult:  # noqa: ANN001
        self.calls.append({"messages": len(messages), "tools": len(kwargs.get("tools") or [])})
        if self._delay:
            await asyncio.sleep(self._delay)
        result = self._script[min(self._index, len(self._script) - 1)]
        self._index += 1
        return result

    async def list_models(self) -> list[str]:
        return [self.model]


def tool_call(name: str, arguments: dict[str, Any], call_id: str = "call-1") -> ChatResult:
    return ChatResult(
        content="",
        model="scripted-model",
        tool_calls=(ToolCall(name=name, arguments=arguments, call_id=call_id),),
        latency_ms=5.0,
    )


def answer(text: str = "listo") -> ChatResult:
    return ChatResult(content=text, model="scripted-model", latency_ms=5.0)


RESERVATION_ARGS = {
    "restaurant": "El Fogón",
    "party_size": 2,
    "local_time": "2026-11-03 21:00",
    "name": "Esteban",
}


# --------------------------------------------------------------------------- #
# A harness that builds the real services
# --------------------------------------------------------------------------- #
class Phase3:
    """Real database, real registry, real approval service, scripted model."""

    def __init__(self, *, script: list[ChatResult], delay: float = 0.0, **overrides: Any) -> None:
        self.settings: Settings = make_db_settings(**overrides)
        self.script = script
        self.delay = delay

    async def __aenter__(self) -> "Phase3":
        truncate_all(self.settings)
        self.database = build_database(self.settings)
        self.checkpointer = await build_checkpointer(self.settings)
        await self.checkpointer.saver.setup()
        self.telegram = RecordingTelegram()
        self.model = ScriptedModel(self.script, delay=self.delay)

        self.memory = MemoryTools(sessions=self.database.sessions)
        self.tasks = TaskTools(
            sessions=self.database.sessions, default_timezone="Europe/Madrid"
        )
        self.restaurants = RestaurantTools()
        self.registry = ToolRegistry(
            [
                *memory_specs(self.memory),
                *task_specs(self.tasks),
                *restaurant_specs(self.restaurants),
            ],
            sessions=self.database.sessions,
        )
        self.approvals = ApprovalService(
            sessions=self.database.sessions, ttl_minutes=self.settings.approval_ttl_minutes
        )
        self.queue = WorkQueue(
            sessions=self.database.sessions, lease_seconds=self.settings.dispatcher_lease_seconds
        )
        self.agent = ChatAgent(
            self.model,
            system_prompt=build_system_prompt(),
            budget_seconds=self.settings.request_budget_seconds,
            checkpointer=self.checkpointer.saver,
            registry=self.registry,
            approvals=self.approvals,
        )
        _dedupe, conversations = build_stores(
            store_backend="postgres",
            app_env="test",
            update_dedupe_ttl_seconds=3600,
            update_dedupe_max_entries=100,
            context_max_messages=20,
            context_ttl_seconds=3600,
            session_factory=self.database.sessions,
            history_provider=self.agent,
        )
        self.conversations = ConversationService(conversations, max_messages=20)
        self.handlers = TelegramHandlers(
            settings=self.settings,
            telegram=self.telegram,
            agent=self.agent,
            conversations=self.conversations,
            memory_tools=self.memory,
            task_tools=self.tasks,
            approvals=self.approvals,
            work_queue=self.queue,
            registry=self.registry,
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.checkpointer.aclose()
        await self.database.dispose()

    def identity(self, user_id: int = USER_ID) -> TelegramIdentity:
        return TelegramIdentity(user_id=user_id, chat_id=CHAT_ID)

    async def sql(self, query: str, **params: Any) -> list[dict[str, Any]]:
        async with self.database.sessions() as session:
            rows = await session.execute(sa.text(query).bindparams(**params))
            return [dict(row) for row in rows.mappings().all()]

    async def action_rows(self) -> list[dict[str, Any]]:
        return await self.sql(
            "SELECT action_id, state, args_revision, args_hash, attempts, result_json, error "
            "FROM pending_actions ORDER BY created_at"
        )

    async def callback_for(self, action_id: str, *, approve: bool = True, revision: int = 1) -> str:
        from app.policy.approval import build_callback_data

        rows = await self.sql(
            "SELECT args_hash FROM pending_actions WHERE action_id = :id", id=action_id
        )
        return build_callback_data(
            action_id=action_id,
            approve=approve,
            args_revision=revision,
            args_hash=rows[0]["args_hash"],
        )


def message_update(update_id: int, text: str = "reserva mesa", user_id: int = USER_ID) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": user_id, "is_bot": False, "first_name": "U"},
            "chat": {"id": CHAT_ID, "type": "private"},
            "date": 1_700_000_000,
            "text": text,
        },
    }


def callback_update(update_id: int, data: str, user_id: int = USER_ID) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb-{update_id}",
            "from": {"id": user_id},
            "message": {"message_id": 500 + update_id, "chat": {"id": CHAT_ID}},
            "data": data,
        },
    }


# --------------------------------------------------------------------------- #
# (a) a level-2 request is prepared, not performed
# --------------------------------------------------------------------------- #
async def test_a_level_two_action_is_prepared_and_never_executed() -> None:
    async with Phase3(script=[tool_call("submit_reservation", RESERVATION_ARGS), answer()]) as p:
        outcome = await p.handlers.handle(message_update(1), p.identity())

    # Precondition: the model really asked for the level-2 tool.
    assert p.model.calls[0]["tools"] >= 1, "the model must have been offered tools"

    rows = await p.action_rows()
    assert len(rows) == 1, "exactly one pending action"
    assert rows[0]["state"] == ActionState.PENDING
    assert rows[0]["attempts"] == 0

    # The approval prompt carries the frozen preview and the two buttons.
    assert len(p.telegram.calls) == 1
    prompt = p.telegram.sent_texts[0]
    assert "submit_reservation" in prompt, "the frozen preview names the exact call"
    buttons = p.telegram.calls[0].kwargs["reply_markup"]["inline_keyboard"][0]
    assert [button["text"] for button in buttons] == ["Aprobar", "Rechazar"]
    assert all(button["callback_data"].startswith("ap:") for button in buttons)

    # And nothing executed: no invocation ledger row, no reserved message, no result.
    assert await p.sql("SELECT 1 FROM tool_invocations") == []
    assert rows[0]["result_json"] is None
    assert outcome.detail == "approval_requested"


# --------------------------------------------------------------------------- #
# (b) one execution under concurrent claims
# --------------------------------------------------------------------------- #
async def test_b_concurrent_claims_execute_exactly_once() -> None:
    async with Phase3(script=[tool_call("submit_reservation", RESERVATION_ARGS), answer()]) as p:
        await p.handlers.handle(message_update(1), p.identity())
        rows = await p.action_rows()
        action_id = rows[0]["action_id"]
        data = await p.callback_for(action_id)

        from app.policy.approval import parse_callback_data

        parsed = parse_callback_data(data)
        assert parsed is not None
        await p.approvals.decide(callback=parsed, user_id=USER_ID, chat_id=CHAT_ID)

        results = await asyncio.gather(
            p.approvals.claim_and_execute(
                callback=parsed, user_id=USER_ID, chat_id=CHAT_ID, executor="a"
            ),
            p.approvals.claim_and_execute(
                callback=parsed, user_id=USER_ID, chat_id=CHAT_ID, executor="b"
            ),
        )

    claimed = [result for result in results if result.claimed]
    refused = [result for result in results if not result.claimed]
    # Precondition: both calls really ran, so the loser genuinely lost the race.
    assert len(claimed) == 1, f"exactly one claim may win, got {len(claimed)}"
    assert len(refused) == 1, "the other call must have been refused, not crashed"
    assert refused[0].message_key in {"approval.in_progress", "approval.already_resolved"}

    attempts = (await p.action_rows())[0]["attempts"]
    assert attempts == 1, "the claim CAS increments attempts once"


async def test_b_a_repeated_callback_is_answered_as_already_handled() -> None:
    """The spec's headline sentence, through the real callback path."""

    async with Phase3(script=[tool_call("submit_reservation", RESERVATION_ARGS), answer()]) as p:
        await p.handlers.handle(message_update(1), p.identity())
        action_id = (await p.action_rows())[0]["action_id"]
        data = await p.callback_for(action_id)

        await p.handlers.handle(callback_update(2, data), p.identity())
        first_state = (await p.action_rows())[0]["state"]

        # The second press of the same button.
        await p.handlers.handle(callback_update(3, data), p.identity())

    assert first_state == ActionState.FAILED, "the level-2 adapter reports the capability gap"
    outcomes = p.telegram.sent_texts
    assert messages.render("approval.capability_unavailable") in outcomes
    assert outcomes[-1] in {
        messages.render("approval.in_progress"),
        messages.render("approval.already_resolved"),
    }, f"the repeat must be reported as already handled, got {outcomes[-1]!r}"
    assert p.telegram.count_of("answerCallbackQuery") == 2, "each press is acknowledged (N2)"

    rows = await p.action_rows()
    assert len(rows) == 1, "no second action was created"
    assert rows[0]["attempts"] == 1, "the action ran once"


# --------------------------------------------------------------------------- #
# (c) wrong user, expired, changed revision
# --------------------------------------------------------------------------- #
async def test_c_another_user_cannot_consume_the_approval() -> None:
    async with Phase3(script=[tool_call("submit_reservation", RESERVATION_ARGS), answer()]) as p:
        await p.handlers.handle(message_update(1), p.identity())
        action_id = (await p.action_rows())[0]["action_id"]
        data = await p.callback_for(action_id)

        # Allow-listed, but not the user the action belongs to.
        await p.handlers.handle(callback_update(2, data, user_id=OTHER_USER), p.identity(OTHER_USER))

    assert p.telegram.sent_texts[-1] == messages.render("approval.wrong_identity")
    rows = await p.action_rows()
    assert rows[0]["state"] == ActionState.PENDING, "nothing was decided or executed"
    assert rows[0]["attempts"] == 0


async def test_c_an_expired_approval_is_refused_and_says_it_expired() -> None:
    async with Phase3(script=[tool_call("submit_reservation", RESERVATION_ARGS), answer()]) as p:
        request = await p.approvals.prepare(
            user_id=USER_ID,
            chat_id=CHAT_ID,
            action_type="submit_reservation",
            args=RESERVATION_ARGS,
            preview_text="submit_reservation(...)",
        )
        # The row is created valid (the CHECK constraint requires expires_at > created_at) and
        # then ages: this is what an approval looks like after its TTL has lapsed.
        async with p.database.sessions() as session, session.begin():
            await session.execute(
                sa.text(
                    "UPDATE pending_actions SET expires_at = now() - interval '1 minute', "
                    "created_at = now() - interval '2 minutes' WHERE action_id = :id"
                ).bindparams(id=request.action_id)
            )
        data = await p.callback_for(request.action_id)

        from app.policy.approval import parse_callback_data

        parsed = parse_callback_data(data)
        assert parsed is not None
        decision = await p.approvals.decide(callback=parsed, user_id=USER_ID, chat_id=CHAT_ID)
        execution = await p.approvals.claim_and_execute(
            callback=parsed, user_id=USER_ID, chat_id=CHAT_ID, executor="test"
        )

    assert not decision.applied
    assert decision.message_key == "approval.expired", (
        "an expired approval must say it expired, not that it is in progress (B4)"
    )
    assert not execution.claimed
    assert execution.message_key == "approval.expired"
    rows = await p.action_rows()
    assert rows[0]["state"] == ActionState.PENDING
    assert rows[0]["attempts"] == 0


async def test_c_a_changed_revision_invalidates_the_approval() -> None:
    async with Phase3(script=[tool_call("submit_reservation", RESERVATION_ARGS), answer()]) as p:
        await p.handlers.handle(message_update(1), p.identity())
        action_id = (await p.action_rows())[0]["action_id"]
        data = await p.callback_for(action_id, revision=1)

        # The permitted re-presentation path: revision + 1 while still pending.
        async with p.database.sessions() as session, session.begin():
            await session.execute(
                sa.text(
                    "UPDATE pending_actions SET args_revision = args_revision + 1, "
                    "args_json = CAST(:args AS jsonb) WHERE action_id = :id"
                ).bindparams(id=action_id, args=json.dumps({**RESERVATION_ARGS, "party_size": 5}))
            )

        await p.handlers.handle(callback_update(2, data), p.identity())

    assert p.telegram.sent_texts[-1] == messages.render("approval.arguments_changed")
    rows = await p.action_rows()
    assert rows[0]["state"] == ActionState.PENDING, "no execution happened"
    assert rows[0]["attempts"] == 0


# --------------------------------------------------------------------------- #
# (d) an ambiguous timeout is `unknown`, never `failed`
# --------------------------------------------------------------------------- #
async def test_d_an_ambiguous_timeout_yields_unknown_not_failed() -> None:
    from app.errors import OllamaTimeout

    async with Phase3(script=[tool_call("submit_reservation", RESERVATION_ARGS), answer()]) as p:
        await p.handlers.handle(message_update(1), p.identity())
        action_id = (await p.action_rows())[0]["action_id"]
        data = await p.callback_for(action_id)

        async def _timeout(**_: Any) -> Any:
            raise OllamaTimeout("provider did not answer in time")

        # The adapter is replaced by one that times out, so the *executor* path is what runs.
        p.registry = p.registry.bind("submit_reservation", _timeout)
        p.handlers._registry = p.registry  # noqa: SLF001 - the wiring's seam

        await p.handlers.handle(callback_update(2, data), p.identity())

    rows = await p.action_rows()
    assert rows[0]["state"] == ActionState.UNKNOWN, (
        "a timeout does not tell us whether the far side acted; `failed` would be a claim"
    )
    assert rows[0]["result_json"] == {"reason": "timeout"}
    assert p.telegram.sent_texts[-1] == messages.render("approval.unknown")
    assert messages.render("approval.failed", detail="x") not in p.telegram.sent_texts


# --------------------------------------------------------------------------- #
# (e) a lost turn is queued, and the resume produces exactly one reply
# --------------------------------------------------------------------------- #
async def test_e_a_lost_turn_is_queued_and_resumed_once() -> None:
    async with Phase3(script=[answer("respuesta reanudada")]) as p:
        prepared = await p.conversations.prepare(
            user_id=USER_ID, chat_id=CHAT_ID, text="primer turno"
        )
        conversation_id = await p.conversations.conversation_id(
            user_id=USER_ID, chat_id=CHAT_ID
        )

        # The turn dies before answering (the budget case): the watchdog is what survives.
        claim = await p.queue.open_turn(
            conversation_id=conversation_id, thread_id=prepared.thread_id, chat_id=CHAT_ID
        )
        assert claim.opened and not claim.is_busy
        state = await p.queue.state_of(thread_id=prepared.thread_id)
        assert state == "pending", "a lost turn must be queued, not dropped"

        # A fresh process (a new agent over the same checkpointer) finishes the thread.
        resumed = await p.agent.run(
            prepared.prompt_input,
            thread_id=prepared.thread_id,
            user_id=USER_ID,
            chat_id=CHAT_ID,
        )
        delivery = await p.telegram.send_message(
            chat_id=CHAT_ID, text=resumed.content or messages.render("error.generic")
        )
        await p.queue.close_turn(thread_id=prepared.thread_id)

    assert delivery.message_id > 0
    assert p.telegram.sent_texts == ["respuesta reanudada"], "exactly one reply"
    assert await p.queue.state_of(thread_id=prepared.thread_id) == "done"
    items = await p.sql("SELECT state FROM work_items")
    assert [item["state"] for item in items] == ["done"], "no orphaned turn is left behind"


# --------------------------------------------------------------------------- #
# (f) the ledger's three cases, distinguishable
# --------------------------------------------------------------------------- #
async def test_f_same_key_replays_from_the_ledger() -> None:
    args = {"title": "Llamar al dentista", "due_local": "2026-11-03 09:30"}

    async with Phase3(script=[answer()]) as p:
        run_key = compose_run_key(
            parent_checkpoint_id="turn1", node_name="run_tools", tool_name="create_reminder",
            call_index=0,
        )
        first = await p.registry.invoke(
            tool_name="create_reminder", arguments=args, thread_id="t1", run_key=run_key,
            attempt_no=1, user_id=USER_ID,
        )
        second = await p.registry.invoke(
            tool_name="create_reminder", arguments=args, thread_id="t1", run_key=run_key,
            attempt_no=1, user_id=USER_ID,
        )
        tasks = await p.sql("SELECT id FROM tasks")

    assert first.ok and not first.meta.get("replayed")
    assert second.meta.get("replayed") is True, "rule 2: an exact match returns the record"
    assert second.data == first.data
    assert len(tasks) == 1, "the side effect happened exactly once"


async def test_f_reworded_arguments_raise_rather_than_replay() -> None:
    args = {"title": "Llamar al dentista", "due_local": "2026-11-03 09:30"}
    reworded = {"title": "Llamar al médico", "due_local": "2026-11-03 09:30"}

    async with Phase3(script=[answer()]) as p:
        run_key = compose_run_key(
            parent_checkpoint_id="turn1", node_name="run_tools", tool_name="create_reminder",
            call_index=0,
        )
        await p.registry.invoke(
            tool_name="create_reminder", arguments=args, thread_id="t1", run_key=run_key,
            attempt_no=1, user_id=USER_ID,
        )

        with pytest.raises(ToolCallArgsMismatch):
            await p.registry.invoke(
                tool_name="create_reminder", arguments=reworded, thread_id="t1", run_key=run_key,
                attempt_no=1, user_id=USER_ID,
            )

        tasks = await p.sql("SELECT title FROM tasks")
        # Zero *additional* side effects: the first attempt is the only one.
        assert [task["title"] for task in tasks] == ["Llamar al dentista"]


async def test_f_a_reordered_pair_cannot_return_the_other_tools_result() -> None:
    """Two halves, because `tool_name` in the key makes the swap an identity change."""

    key_a = compose_run_key(
        parent_checkpoint_id="turn1", node_name="run_tools", tool_name="create_reminder",
        call_index=0,
    )
    key_b = compose_run_key(
        parent_checkpoint_id="turn1", node_name="run_tools", tool_name="save_preference",
        call_index=0,
    )
    assert key_a != key_b, "a swapped ordinal denotes a different key, so no cross-tool replay"

    async with Phase3(script=[answer()]) as p:
        # And if a caller ever reuses one key for a different tool, the hash mismatch is loud.
        await p.registry.invoke(
            tool_name="create_reminder",
            arguments={"title": "X", "due_local": "2026-11-03 09:30"},
            thread_id="t1",
            run_key=key_a,
            attempt_no=1,
            user_id=USER_ID,
        )
        with pytest.raises(ToolCallArgsMismatch):
            await p.registry.invoke(
                tool_name="save_preference",
                arguments={"key": "ciudad", "value": "Madrid"},
                thread_id="t1",
                run_key=key_a,
                attempt_no=1,
                user_id=USER_ID,
            )


async def test_f_two_deliberate_identical_requests_both_execute() -> None:
    """Different turns produce different keys, so a legitimate repeat is not suppressed."""

    args = {"title": "Regar las plantas", "due_local": "2026-11-03 09:30"}

    async with Phase3(script=[answer()]) as p:
        keys = [
            compose_run_key(
                parent_checkpoint_id=f"turn{turn}", node_name="run_tools",
                tool_name="create_reminder", call_index=0,
            )
            for turn in (1, 2)
        ]
        assert len(set(keys)) == 2, "two turns must be two keys"

        outcomes = [
            await p.registry.invoke(
                tool_name="create_reminder", arguments=args, thread_id="t1", run_key=run_key,
                attempt_no=1, user_id=USER_ID,
            )
            for run_key in keys
        ]

        ledger = await p.sql(
            "SELECT run_key, state FROM tool_invocations ORDER BY id"
        )
        tasks = await p.sql("SELECT id FROM tasks")

    # The ledger ran both calls: two rows, two different keys, neither replayed. This is the
    # property A1's fourth case is about — a deliberate repeat is not suppressed by the ledger.
    assert len(ledger) == 2, f"both turns must reach the tool, got {ledger}"
    assert len({row["run_key"] for row in ledger}) == 2, "two turns are two keys"
    assert all(row["state"] == "succeeded" for row in ledger)
    assert not any(outcome.meta.get("replayed") for outcome in outcomes)

    # And the tool's *own* content idempotency then collapses the duplicate reminder to one
    # task: two mechanisms, two different guarantees (test_create_reminder_is_idempotent_by_content
    # in test_phase2_db.py asserts that one). The ledger's job is not to suppress it.
    assert len(tasks) == 1


# --------------------------------------------------------------------------- #
# (g) the zero-row diagnosis names the actual cause
# --------------------------------------------------------------------------- #
def test_g_the_seven_zero_row_keys_are_distinct_and_all_exist() -> None:
    assert len(ZERO_ROW_KEYS) == 7, "§4.5 has seven rows after the RB2 merge"
    assert len(set(ZERO_ROW_KEYS)) == 7, "one key per cause, never two causes on one key"
    for key in ZERO_ROW_KEYS:
        assert messages.has(key), f"{key} is missing from the catalogue"


async def test_g_a_zero_row_claim_reports_the_rows_real_cause() -> None:
    """The same shape, four different causes, four different messages."""

    async with Phase3(script=[answer()]) as p:
        from app.policy.approval import parse_callback_data

        # 1. An expired row.
        expired = await p.approvals.prepare(
            user_id=USER_ID, chat_id=CHAT_ID, action_type="submit_reservation",
            args={**RESERVATION_ARGS, "party_size": 4}, preview_text="x",
        )
        async with p.database.sessions() as session, session.begin():
            await session.execute(
                sa.text(
                    "UPDATE pending_actions SET expires_at = now() - interval '1 minute', "
                    "created_at = now() - interval '2 minutes' WHERE action_id = :id"
                ).bindparams(id=expired.action_id)
            )
        # 2. An unknown action id.
        forged = parse_callback_data(
            f"ap:does-not-exist:a:1:{'0' * 16}"
        )
        # 3. A pending row whose revision moved on.
        moved = await p.approvals.prepare(
            user_id=USER_ID, chat_id=CHAT_ID, action_type="submit_reservation",
            args={**RESERVATION_ARGS, "party_size": 3}, preview_text="y",
        )
        async with p.database.sessions() as session, session.begin():
            await session.execute(
                sa.text(
                    "UPDATE pending_actions SET args_revision = args_revision + 1, "
                    "args_json = CAST(:args AS jsonb) WHERE action_id = :id"
                ).bindparams(id=moved.action_id, args=json.dumps({**RESERVATION_ARGS, "party_size": 6}))
            )
        stale_button = parse_callback_data(
            f"ap:{moved.action_id}:a:1:{'0' * 16}"
        )

        results = {}
        for name, parsed, action_id in (
            ("expired", parse_callback_data(await p.callback_for(expired.action_id)), expired.action_id),
            ("unknown", forged, "does-not-exist"),
        ):
            assert parsed is not None
            outcome = await p.approvals.claim_and_execute(
                callback=parsed, user_id=USER_ID, chat_id=CHAT_ID, executor="test"
            )
            results[name] = outcome.message_key

        assert stale_button is not None
        outcome = await p.approvals.claim_and_execute(
            callback=stale_button, user_id=USER_ID, chat_id=CHAT_ID, executor="test"
        )
        results["moved"] = outcome.message_key

    assert results["expired"] == "approval.expired"
    assert results["unknown"] == "approval.unknown_action"
    assert results["moved"] == "approval.arguments_changed"
    assert len(set(results.values())) == 3, f"causes must not share a message: {results}"


# --------------------------------------------------------------------------- #
# (h) two quick messages
# --------------------------------------------------------------------------- #
async def test_h_two_quick_messages_are_declined_without_a_second_graph_run() -> None:
    """The first turn is genuinely in flight when the second arrives."""

    async with Phase3(script=[answer("primera respuesta")], delay=1.5) as p:
        # Both messages are handled concurrently, so the second arrives mid-turn.
        first, second = await asyncio.gather(
            p.handlers.handle(message_update(1, "primer mensaje"), p.identity()),
            p.handlers.handle(message_update(2, "segundo mensaje"), p.identity()),
        )

        items = await p.sql("SELECT thread_id, state FROM work_items")
        inbox = await p.sql("SELECT update_id, status FROM inbox_updates ORDER BY update_id")

    # Which of the two messages wins the enqueue race is not determined by their launch order,
    # so the assertion is the *property*: exactly one turn runs and exactly one is declined.
    details = sorted([first.detail, second.detail])
    assert details == ["answered", "busy_previous_turn"], (
        f"exactly one turn must be declined and one answered, got {details}"
    )

    busy = messages.render("busy.previous_turn_in_progress")
    assert p.telegram.sent_texts.count(busy) == 1, "exactly one busy reply"
    assert "primera respuesta" in p.telegram.sent_texts

    assert len(p.model.calls) == 1, "the declined message must not reach the model"
    assert len(items) == 1, "no second watchdog row"
    assert [item["state"] for item in items] == ["done"], "no orphaned turn"

    # The inbox is written by the *webhook*, not by the handler, so this test drives the
    # handler directly and cannot assert on it; the webhook-level assertion lives in
    # tests/integration/test_phase3_webhook.py::test_h_... (same criterion, one layer up).
    assert inbox == [], "this seam does not write the inbox: the webhook does"
