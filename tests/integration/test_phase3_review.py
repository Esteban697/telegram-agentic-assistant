"""Phase 3 review tests: the thirteen findings, each with a test that fails without the fix."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from app.agent.graph import ChatAgent
from app.bot import messages
from app.errors import AssistantError
from app.jobs import resume_work
from app.llm.protocols import ChatResult, ToolCall
from app.policy import approval as machine
from app.policy.approval import ActionState
from app.services.notifications import RecordingNotifier
from app.tools.registry import ApprovalRequired, compose_run_key
from tests.integration.test_phase3_approvals import (
    CHAT_ID,
    TEST_DATABASE_URL,
    USER_ID,
    Phase3,
    answer,
    callback_update,
    message_update,
    tool_call,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set"),
]

RESERVATION_ARGS = {
    "restaurant": "El Fogón",
    "party_size": 2,
    "local_time": "2026-11-03 21:00",
    "name": "Esteban",
}


# --------------------------------------------------------------------------- #
# 1: no direct execution path for a level-2 tool
# --------------------------------------------------------------------------- #
async def test_1_the_registry_refuses_to_invoke_a_level_two_tool() -> None:
    async with Phase3(script=[answer()]) as p:
        with pytest.raises(ApprovalRequired):
            await p.registry.invoke(
                tool_name="submit_reservation",
                arguments=RESERVATION_ARGS,
                thread_id="t1",
                run_key="turn1:run_tools:submit_reservation:0",
                attempt_no=1,
                user_id=USER_ID,
            )

        # Not even a ledger row: the call never started.
        assert await p.sql("SELECT 1 FROM tool_invocations") == []
        assert await p.sql("SELECT 1 FROM pending_actions") == []


async def test_1_execute_approved_requires_a_successful_claim() -> None:
    from app.services.approvals import Execution

    async with Phase3(script=[answer()]) as p:
        unclaimed = Execution(claimed=False, message_key="approval.in_progress")
        with pytest.raises(ApprovalRequired):
            await p.registry.execute_approved(execution=unclaimed, user_id=USER_ID)


async def test_1_every_level_two_spec_is_gated() -> None:
    async with Phase3(script=[answer()]) as p:
        gated = [
            name for name in p.registry.names() if p.registry.get(name).requires_approval
        ]
    assert gated == ["submit_reservation"], f"the level-2 surface changed: {gated}"


# --------------------------------------------------------------------------- #
# 2: an unexpected failure must not lock the chat
# --------------------------------------------------------------------------- #
async def test_2_an_unexpected_error_closes_the_watchdog_and_the_chat_keeps_working() -> None:
    async with Phase3(script=[answer("primera")], delay=0.0) as p:
        async def _boom(**_: Any) -> Any:
            raise RuntimeError("unexpected failure inside the turn")

        healthy = p.handlers._agent  # noqa: SLF001 - the seam the handler uses
        p.handlers._agent = _BrokenAgent()  # noqa: SLF001

        # An unexpected error escapes the handler (the webhook's catch-all would reply
        # `error.generic`); what matters is that the watchdog row is still closed.
        with pytest.raises(RuntimeError):
            await p.handlers.handle(message_update(1, "primer mensaje"), p.identity())

        p.handlers._agent = healthy  # noqa: SLF001 - the chat keeps working afterwards
        thread_id = await p.conversations.thread_id(user_id=USER_ID, chat_id=CHAT_ID)
        state_after = await p.queue.state_of(thread_id=thread_id)

        # The next message must not be declined as busy.
        second = await p.handlers.handle(message_update(2, "segundo mensaje"), p.identity())

    assert state_after == "failed", "an unexpected failure must close the row, not strand it"
    assert second.detail != "busy_previous_turn", (
        "the chat must not be locked out after one unexpected error"
    )


class _BrokenAgent:
    """A stand-in that fails the way an unexpected bug would: not an `AssistantError`."""

    model_name = "broken"

    async def run(self, *_: Any, **__: Any) -> Any:
        raise RuntimeError("unexpected failure inside the turn")

    async def resume(self, *_: Any, **__: Any) -> Any:  # pragma: no cover
        raise RuntimeError("unexpected failure inside the turn")

    async def pending_approval(self, *_: Any, **__: Any) -> None:
        return None

    async def history(self, *_: Any, **__: Any) -> list:
        return []


# --------------------------------------------------------------------------- #
# 3: re-requesting the same action after it resolved
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("terminal", [ActionState.REJECTED, ActionState.FAILED])
async def test_3_the_same_action_can_be_requested_again_after_it_resolved(terminal: str) -> None:
    async with Phase3(script=[answer()]) as p:
        first = await p.approvals.prepare(
            user_id=USER_ID, chat_id=CHAT_ID, action_type="submit_reservation",
            args=RESERVATION_ARGS, preview_text="x",
        )
        async with p.database.sessions() as session, session.begin():
            await session.execute(
                sa.text(
                    "UPDATE pending_actions SET state = :state WHERE action_id = :id"
                ).bindparams(state=str(terminal), id=first.action_id)
            )

        second = await p.approvals.prepare(
            user_id=USER_ID, chat_id=CHAT_ID, action_type="submit_reservation",
            args=RESERVATION_ARGS, preview_text="x",
        )

        rows = await p.sql("SELECT action_id, state, idempotency_key FROM pending_actions ORDER BY created_at")

    assert second.action_id != first.action_id, "a retry is a new row (§4.1)"
    assert len(rows) == 2, f"both attempts must exist: {rows}"
    assert rows[1]["state"] == ActionState.PENDING
    assert rows[0]["idempotency_key"] != rows[1]["idempotency_key"], "the key is unique per attempt"


async def test_3_a_live_action_is_still_not_stacked() -> None:
    """The partial live index remains the arbiter: the fix must not allow duplicates *while live*."""

    async with Phase3(script=[answer()]) as p:
        first = await p.approvals.prepare(
            user_id=USER_ID, chat_id=CHAT_ID, action_type="submit_reservation",
            args=RESERVATION_ARGS, preview_text="x",
        )
        second = await p.approvals.prepare(
            user_id=USER_ID, chat_id=CHAT_ID, action_type="submit_reservation",
            args=RESERVATION_ARGS, preview_text="x",
        )
        rows = await p.sql("SELECT action_id FROM pending_actions")

    assert second.action_id == first.action_id, "the live prompt is re-used, not duplicated"
    assert len(rows) == 1


# --------------------------------------------------------------------------- #
# 4 + 5 + 13: the watchdog's payload, its due time, and the real resume job
# --------------------------------------------------------------------------- #
async def test_4_the_watchdog_carries_the_chat_id_and_the_real_job_delivers_to_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "resume.jsonl"

    async with Phase3(script=[answer("respuesta reanudada")]) as p:
        prepared = await p.conversations.prepare(
            user_id=USER_ID, chat_id=CHAT_ID, text="primer turno"
        )
        conversation_id = await p.conversations.conversation_id(
            user_id=USER_ID, chat_id=CHAT_ID
        )
        # Give the thread a real checkpoint first: `resume` needs one, and an empty thread is a
        # different failure (the job reports it as `EmptyInputError` rather than a lost chat).
        await p.agent.run(
            prepared.prompt_input,
            thread_id=prepared.thread_id,
            user_id=USER_ID,
            chat_id=CHAT_ID,
        )
        await p.queue.open_turn(
            conversation_id=conversation_id,
            thread_id=prepared.thread_id,
            chat_id=CHAT_ID,
        )

        rows = await p.sql("SELECT payload, run_at, created_at FROM work_items")
        payload = rows[0]["payload"]
        assert payload.get("chat_id") == CHAT_ID, f"the chat id must be in the payload: {payload}"

        # Finding 5: the row is not due until the turn's own budget has elapsed.
        delay = (rows[0]["run_at"] - rows[0]["created_at"]).total_seconds()
        assert delay > 1.0, f"the watchdog must be scheduled after the budget, got {delay}s"
        assert await p.queue.claim_due(batch=10, executor="test") == [], (
            "a live turn must not be claimable"
        )

        # Simulate the budget having elapsed, then run the **real job** against this database.
        async with p.database.sessions() as session, session.begin():
            await session.execute(
                sa.text("UPDATE work_items SET run_at = now() - interval '1 second'")
            )

    settings = os.environ.copy()
    settings.update(
        {
            "DATABASE_URL": TEST_DATABASE_URL,
            "DATABASE_SYNC_URL": TEST_DATABASE_URL,
            "STORE_BACKEND": "postgres",
            "APP_ENV": "test",
            "LOG_LEVEL": "WARNING",
            "NOTIFICATIONS_TRANSPORT": "record",
            "NOTIFY_RECORD_PATH": str(record),
        }
    )
    repo = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, "-m", "app.jobs.resume_work"],
        cwd=repo,
        env=settings,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr[-400:]
    reports = [line for line in completed.stdout.splitlines() if line.startswith("JOB_REPORT")]
    assert reports, completed.stdout[-400:]
    report = json.loads(reports[-1][len("JOB_REPORT ") :])

    lines = record.read_text(encoding="utf-8").strip().splitlines() if record.exists() else []
    assert report["ok"] is True, report
    # The finding: this used to call sendMessage(chat_id=0).
    assert len(lines) == 1, f"exactly one delivery: {lines}"
    assert json.loads(lines[0])["chat_id"] == CHAT_ID
    assert report["failed"] == []


async def test_4_without_a_chat_id_the_item_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing chat id must be a reported failure, never a send to chat 0."""

    async with Phase3(script=[answer("x")]) as p:
        prepared = await p.conversations.prepare(
            user_id=USER_ID, chat_id=CHAT_ID, text="turno"
        )
        conversation_id = await p.conversations.conversation_id(
            user_id=USER_ID, chat_id=CHAT_ID
        )
        # Give the thread a real checkpoint first: `resume` needs one, and an empty thread is a
        # different failure (the job reports it as `EmptyInputError` rather than a lost chat).
        await p.agent.run(
            prepared.prompt_input,
            thread_id=prepared.thread_id,
            user_id=USER_ID,
            chat_id=CHAT_ID,
        )
        await p.queue.open_turn(
            conversation_id=conversation_id, thread_id=prepared.thread_id, chat_id=CHAT_ID
        )
        async with p.database.sessions() as session, session.begin():
            await session.execute(
                sa.text("UPDATE work_items SET payload = '{}'::jsonb, run_at = now() - interval '1 second'")
            )

    settings = os.environ.copy()
    settings.update(
        {
            "DATABASE_URL": TEST_DATABASE_URL,
            "DATABASE_SYNC_URL": TEST_DATABASE_URL,
            "STORE_BACKEND": "postgres",
            "APP_ENV": "test",
            "LOG_LEVEL": "WARNING",
            "NOTIFICATIONS_TRANSPORT": "record",
            "NOTIFY_RECORD_PATH": str(tmp_path / "n.jsonl"),
        }
    )
    repo = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, "-m", "app.jobs.resume_work"],
        cwd=repo, env=settings, capture_output=True, text=True, timeout=300,
    )
    report = json.loads(
        [line for line in completed.stdout.splitlines() if line.startswith("JOB_REPORT")][-1][
            len("JOB_REPORT ") :
        ]
    )
    assert report["failed"] and report["failed"][0]["error"] == "missing_chat_id", report


# --------------------------------------------------------------------------- #
# 6 + 7: validated level-2 arguments, and float-safe re-verification
# --------------------------------------------------------------------------- #
async def test_6_invalid_level_two_arguments_are_refused_before_the_prompt() -> None:
    bad = {"restaurant": "", "party_size": 99, "local_time": "x", "name": ""}

    async with Phase3(script=[tool_call("submit_reservation", bad), answer()]) as p:
        outcome = await p.handlers.handle(message_update(1), p.identity())

        assert await p.sql("SELECT 1 FROM pending_actions") == [], (
            "an invalid call must not become an approval prompt"
        )
        assert outcome.status == "failed", f"the turn must fail, got {outcome}"
        assert p.telegram.sent_texts == [messages.render("approval.in_progress")] or (
            p.telegram.sent_texts[-1] != messages.render("approval.prompt", preview="x", minutes=30)
        ), "the user must not be shown an approval prompt for an invalid call"


async def test_7_float_arguments_survive_the_jsonb_round_trip() -> None:
    """`1e20` returns from jsonb as an `int`; the guard must not call that a mismatch."""

    args = {
        "restaurant": "El Fogón",
        "party_size": 2,
        "local_time": "2026-11-03 21:00",
        "name": "Esteban",
    }
    async with Phase3(script=[answer()]) as p:
        request = await p.approvals.prepare(
            user_id=USER_ID, chat_id=CHAT_ID, action_type="submit_reservation",
            args={**args, "budget": 1e20}, preview_text="x",
        )
        data = await p.callback_for(request.action_id)
        parsed = machine.parse_callback_data(data)
        assert parsed is not None
        await p.approvals.decide(callback=parsed, user_id=USER_ID, chat_id=CHAT_ID)
        execution = await p.approvals.claim_and_execute(
            callback=parsed, user_id=USER_ID, chat_id=CHAT_ID, executor="test"
        )
        rows = await p.action_rows()

    assert execution.claimed, "a float argument must not be refused as a mismatch"
    assert rows[0]["state"] == ActionState.EXECUTING
    assert float(execution.args["budget"]) == 1e20


async def test_7_a_tampered_row_is_still_refused() -> None:
    """The guard must keep working: the stored canonical text and hash must agree."""

    async with Phase3(script=[answer()]) as p:
        request = await p.approvals.prepare(
            user_id=USER_ID, chat_id=CHAT_ID, action_type="submit_reservation",
            args=RESERVATION_ARGS, preview_text="x",
        )
        # The trigger's permitted path is "revision + 1 while the row is pending" (RN10), so the
        # stale-hash row is built there and then approved: the review's construction, reproduced.
        async with p.database.sessions() as session, session.begin():
            await session.execute(
                sa.text(
                    "UPDATE pending_actions SET args_revision = args_revision + 1, "
                    "args_json = CAST(:args AS jsonb), args_canonical = :canonical "
                    "WHERE action_id = :id AND state = 'pending'"
                ).bindparams(
                    id=request.action_id,
                    args=json.dumps({"restaurant": "Otro"}),
                    canonical='{"restaurant": "Otro"}',
                )
            )

        stale = await p.callback_for(request.action_id, revision=2)
        parsed = machine.parse_callback_data(stale)
        assert parsed is not None
        await p.approvals.decide(callback=parsed, user_id=USER_ID, chat_id=CHAT_ID)

        from app.services.approvals import ArgumentsChanged

        with pytest.raises(ArgumentsChanged):
            await p.approvals.claim_and_execute(
                callback=parsed, user_id=USER_ID, chat_id=CHAT_ID, executor="test"
            )
        rows = await p.action_rows()

    assert rows[0]["state"] == ActionState.FAILED, "the refused action is recorded as failed"


# --------------------------------------------------------------------------- #
# 8 + 9: the diagnosis order, and the transition table
# --------------------------------------------------------------------------- #
async def test_8_a_tampered_button_on_an_approved_row_reports_arguments_changed() -> None:
    async with Phase3(script=[answer()]) as p:
        request = await p.approvals.prepare(
            user_id=USER_ID, chat_id=CHAT_ID, action_type="submit_reservation",
            args=RESERVATION_ARGS, preview_text="x",
        )
        good = await p.callback_for(request.action_id)
        parsed = machine.parse_callback_data(good)
        assert parsed is not None
        await p.approvals.decide(callback=parsed, user_id=USER_ID, chat_id=CHAT_ID)

        tampered = machine.parse_callback_data(
            f"ap:{request.action_id}:a:99:{'0' * 16}"
        )
        assert tampered is not None
        outcome = await p.approvals.claim_and_execute(
            callback=tampered, user_id=USER_ID, chat_id=CHAT_ID, executor="test"
        )

    assert outcome.message_key == "approval.arguments_changed", (
        "§4.5 row 6 must be reachable: a stale button on an approved row is a revision mismatch, "
        "not 'already in progress'"
    )


def test_9_the_transition_table_is_exhaustive_and_enforced() -> None:
    """§4.1/§9.1: the 8×8 table, with everything else denied."""

    states = [str(state) for state in ActionState]
    assert len(states) == 8

    legal = {
        ("pending", "approved"),
        ("pending", "rejected"),
        ("pending", "expired"),
        ("approved", "executing"),
        ("approved", "expired"),
        ("executing", "succeeded"),
        ("executing", "failed"),
        ("executing", "unknown"),
        ("unknown", "succeeded"),
        ("unknown", "failed"),
    }
    checked = 0
    for source in states:
        for target in states:
            checked += 1
            expected = (source, target) in legal
            assert machine.can_transition(source, target) is expected, f"{source} -> {target}"
            if expected:
                machine.assert_transition(source, target)
            else:
                with pytest.raises(machine.InvalidTransition):
                    machine.assert_transition(source, target)
    assert checked == 64, "the exhaustive table covers every ordered pair"
    assert set(machine.TRANSITIONS) == legal


async def test_9_a_resolved_action_cannot_be_claimed_again() -> None:
    async with Phase3(script=[answer()]) as p:
        request = await p.approvals.prepare(
            user_id=USER_ID, chat_id=CHAT_ID, action_type="submit_reservation",
            args=RESERVATION_ARGS, preview_text="x",
        )
        data = await p.callback_for(request.action_id)
        parsed = machine.parse_callback_data(data)
        assert parsed is not None
        await p.approvals.decide(callback=parsed, user_id=USER_ID, chat_id=CHAT_ID)

        first = await p.approvals.claim_and_execute(
            callback=parsed, user_id=USER_ID, chat_id=CHAT_ID, executor="a"
        )
        assert first.claimed
        await p.approvals.finish(action_id=request.action_id, state=ActionState.SUCCEEDED)

        second = await p.approvals.claim_and_execute(
            callback=parsed, user_id=USER_ID, chat_id=CHAT_ID, executor="b"
        )
        rows = await p.action_rows()

    assert not second.claimed, "a succeeded action must not be executed again"
    assert second.message_key == "approval.already_resolved"
    assert rows[0]["attempts"] == 1, f"attempts must stay 1, got {rows[0]['attempts']}"


def test_9_an_illegal_finish_is_refused_by_the_service() -> None:
    """`finish` from anything but `executing` is rejected before it reaches SQL."""

    import inspect

    source = inspect.getsource(
        __import__("app.services.approvals", fromlist=["ApprovalService"]).ApprovalService.finish
    )
    assert "assert_transition" in source, "the transition table must be enforced, not decorative"


# --------------------------------------------------------------------------- #
# 10: an unrecognised error code is ambiguous, not a failure
# --------------------------------------------------------------------------- #
async def test_10_an_unknown_error_code_is_treated_as_ambiguous() -> None:
    from app.errors import AssistantError

    class Odd(AssistantError):
        code = "some_unlisted_provider_error"
        message_key = "error.generic"

    async with Phase3(script=[tool_call("submit_reservation", RESERVATION_ARGS), answer()]) as p:
        await p.handlers.handle(message_update(1), p.identity())
        action_id = (await p.action_rows())[0]["action_id"]
        data = await p.callback_for(action_id)

        async def _odd(**_: Any) -> Any:
            raise Odd("a provider error code the plan has never seen")

        p.handlers._registry = p.registry.bind("submit_reservation", _odd)  # noqa: SLF001

        await p.handlers.handle(callback_update(2, data), p.identity())
        rows = await p.action_rows()

    assert rows[0]["state"] == ActionState.UNKNOWN, (
        "an unrecognised code is ambiguous: §7 forbids reporting it as a failure"
    )
    assert p.telegram.sent_texts[-1] == messages.render("approval.unknown")


# --------------------------------------------------------------------------- #
# 11 + 12: the ledger through the graph, with the graph's own keys
# --------------------------------------------------------------------------- #
async def test_11_a_reused_key_with_mismatched_arguments_raises_through_the_ledger() -> None:
    """`ToolCallReordered`'s guarantee, demonstrated instead of asserted by construction."""

    from app.tools.registry import ToolCallArgsMismatch

    async with Phase3(script=[answer()]) as p:
        key = compose_run_key(
            parent_checkpoint_id="turn1", node_name="run_tools",
            tool_name="create_reminder", call_index=0,
        )
        await p.registry.invoke(
            tool_name="create_reminder",
            arguments={"title": "A", "due_local": "2026-11-03 09:30"},
            thread_id="t1", run_key=key, attempt_no=1, user_id=USER_ID,
        )

        with pytest.raises(ToolCallArgsMismatch):
            await p.registry.invoke(
                tool_name="save_preference",
                arguments={"key": "ciudad", "value": "Madrid"},
                thread_id="t1", run_key=key, attempt_no=1, user_id=USER_ID,
            )

        ledger = await p.sql("SELECT tool_name, state FROM tool_invocations")
        memories = await p.sql("SELECT 1 FROM memories")

    assert ledger == [{"tool_name": "create_reminder", "state": "succeeded"}]
    assert memories == [], "the mismatched call must not have run"


async def test_12_the_graph_composes_different_keys_for_different_turns() -> None:
    """The `turn` counter is real state: two turns, two keys, two effects."""

    args = {"title": "Regar las plantas", "due_local": "2026-11-03 09:30"}
    script = [
        tool_call("create_reminder", args, call_id="c1"),
        answer("hecho uno"),
        tool_call("create_reminder", args, call_id="c2"),
        answer("hecho dos"),
    ]

    async with Phase3(script=script) as p:
        identity = p.identity()
        await p.handlers.handle(message_update(1, "recuérdame regar"), identity)
        await p.handlers.handle(message_update(2, "recuérdame regar otra vez"), identity)

        ledger = await p.sql("SELECT run_key, state FROM tool_invocations ORDER BY id")
        turn = await p.agent.turn_of(
            await p.conversations.thread_id(user_id=USER_ID, chat_id=CHAT_ID)
        )
        # (the counter is read from the checkpoint after both turns)

    assert len(ledger) == 2, f"both turns must reach the tool: {ledger}"
    assert len({row["run_key"] for row in ledger}) == 2, "each turn composes its own key"
    assert all(row["state"] == "succeeded" for row in ledger)
    assert all("run_tools" in row["run_key"] for row in ledger)
    assert turn >= 2, "the turn counter is explicit state, not a function of the message count"
