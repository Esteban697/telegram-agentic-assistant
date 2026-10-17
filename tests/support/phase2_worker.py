"""Test-support workers: real processes the acceptance criteria are demonstrated with.

**Test code, not application code.** Three criteria cannot be shown inside one interpreter:

* (b) memories and tasks read after a process restart;
* (c) a graph run resumed in a **different process**;
* (d) two dispatchers racing for the same occurrence.

Each is driven here as a separate `python -m tests.support.phase2_worker <command>` process, so
the evidence is a process boundary rather than a same-process simulation.

Every command prints one `WORKER {...}` JSON line and exits non-zero on failure.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from app.agent.graph import ChatAgent
from app.agent.prompts import build_system_prompt
from app.config import load_settings
from app.llm.protocols import ChatMessage, ChatResult
from app.storage import repositories
from app.storage.checkpointing import build_checkpointer, setup_checkpointer
from app.storage.postgres import build_database, use_compatible_event_loop
from app.tools.memory import MemoryTools, SavePreferenceArgs
from app.tools.tasks import CreateReminderArgs, TaskTools

USER_ID = 42
CHAT_ID = 4242


class StubModel:
    """A deterministic chat model, so the workers need no credentials."""

    model = "stub-model"

    def __init__(self, reply: str = "respuesta del modelo") -> None:
        self._reply = reply
        self.calls = 0

    async def chat(self, messages, **_: Any) -> ChatResult:  # noqa: ANN001
        self.calls += 1
        return ChatResult(content=self._reply, model=self.model, latency_ms=1.0)

    async def list_models(self) -> list[str]:
        return [self.model]


def _emit(payload: dict[str, Any]) -> None:
    print("WORKER " + json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


# --------------------------------------------------------------------------- #
# (b) memories and tasks survive a restart
# --------------------------------------------------------------------------- #
async def seed() -> dict[str, Any]:
    settings = load_settings().validate_or_raise()
    database = build_database(settings)
    try:
        memories = MemoryTools(sessions=database.sessions)
        tasks = TaskTools(sessions=database.sessions, max_attempts=settings.reminder_max_attempts)

        await memories.save_preference(
            user_id=USER_ID, args=SavePreferenceArgs(key="ciudad", value="Madrid")
        )
        created = await tasks.create_reminder(
            user_id=USER_ID,
            args=CreateReminderArgs(
                title="Llamar al dentista",
                due_local=(datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%d 10:00"),
                timezone="Europe/Madrid",
            ),
        )
        return {"seeded": True, "task": (created.data or {}).get("task_id")}
    finally:
        await database.dispose()


async def read_seed() -> dict[str, Any]:
    settings = load_settings().validate_or_raise()
    database = build_database(settings)
    try:
        async with database.sessions() as session:
            memories = await repositories.list_memories(session, user_id=USER_ID)
            tasks = await repositories.list_tasks(session, user_id=USER_ID)
            return {
                "memories": [{"key": m.key, "value": m.value} for m in memories],
                "tasks": [{"title": t.title, "state": t.state} for t in tasks],
            }
    finally:
        await database.dispose()


# --------------------------------------------------------------------------- #
# (c) interrupt after a checkpoint, resume in a new process
# --------------------------------------------------------------------------- #
async def _agent_for(settings, *, interrupt: bool) -> tuple[ChatAgent, StubModel, Any]:
    """Build an agent over the durable checkpointer.

    `interrupt` stops before the model node of the *next* turn. Combined with a turn that has
    already completed in the same thread, the checkpoint the interrupting process leaves behind
    contains a **finished step**, so the resuming process proves continuation past a step rather
    than merely "a process started again" (item 8).
    """

    checkpointer = await build_checkpointer(settings)
    model = StubModel(reply="respuesta tras reanudar")
    agent = ChatAgent(
        model,
        system_prompt=build_system_prompt(),
        budget_seconds=settings.request_budget_seconds,
        checkpointer=checkpointer.saver,
        interrupt_before=["call_model"] if interrupt else None,
    )
    return agent, model, checkpointer


async def interrupt(thread_id: str) -> dict[str, Any]:
    """Turn 1 completes for real; turn 2 stops before its model call."""

    settings = load_settings().validate_or_raise()
    database = build_database(settings)
    agent, model, checkpointer = await _agent_for(settings, interrupt=False)
    try:
        await setup_checkpointer(checkpointer)
        first = await agent.run(
            [ChatMessage(role="user", content="primer turno, completo")], thread_id=thread_id
        )

        # A second agent over the same saver, compiled to stop before the model node: turn 2
        # halts with turn 1's finished step already in the checkpoint.
        halting, _, _ = await _agent_for(settings, interrupt=True)
        # Reuse the same saver so both agents share one thread namespace.
        halting = ChatAgent(
            model,
            system_prompt=build_system_prompt(),
            budget_seconds=settings.request_budget_seconds,
            checkpointer=checkpointer.saver,
            interrupt_before=["call_model"],
        )
        # Exactly what the handler does: read the retained history from the checkpoint and pass
        # it plus this turn's message, so the graph state accumulates instead of being replaced.
        prior = await halting.history(thread_id)
        second = await halting.run(
            [*prior, ChatMessage(role="user", content="segundo turno, se interrumpe")],
            thread_id=thread_id,
        )

        interrupted = await halting.is_interrupted(thread_id)
        history = await halting.history(thread_id)
        return {
            "thread_id": thread_id,
            "interrupted": interrupted,
            "first_turn_reply": first.content,
            "second_turn_reply_so_far": second.content,
            "history_len": len(history),
            "history_roles": [message.role for message in history],
            "model_calls_in_this_process": model.calls,
            "process": "interrupting",
        }
    finally:
        await checkpointer.aclose()
        await database.dispose()


async def resume(thread_id: str) -> dict[str, Any]:
    """A new process continues the thread from the checkpoint.

    `model_calls_in_this_process` must be 1: exactly the interrupted turn is executed, and the
    turn that had already completed is **not** replayed.
    """

    settings = load_settings().validate_or_raise()
    database = build_database(settings)
    agent, model, checkpointer = await _agent_for(settings, interrupt=False)
    try:
        result = await agent.resume(thread_id)
        interrupted_after = await agent.is_interrupted(thread_id)
        history = await agent.history(thread_id)
        return {
            "thread_id": thread_id,
            "reply": result.content,
            "still_interrupted": interrupted_after,
            "history": [{"role": m.role, "content": m.content} for m in history],
            "model_calls_in_this_process": model.calls,
            "process": "resuming",
        }
    finally:
        await checkpointer.aclose()
        await database.dispose()


# --------------------------------------------------------------------------- #
# (d) one due task, for the concurrency race
# --------------------------------------------------------------------------- #
async def seed_due() -> dict[str, Any]:
    settings = load_settings().validate_or_raise()
    database = build_database(settings)
    try:
        async with database.sessions() as session, session.begin():
            task, created = await repositories.create_task(
                session,
                user_id=USER_ID,
                title="Recordatorio vencido",
                description=None,
                due_at=datetime.now(UTC) - timedelta(minutes=5),
                timezone="Europe/Madrid",
                idempotency_key="phase2:due",
                max_attempts=3,
            )
            return {"task_id": str(task.id), "created": created}
    finally:
        await database.dispose()


# --------------------------------------------------------------------------- #
# (g) the trigger must refuse a direct UPDATE of approved arguments
# --------------------------------------------------------------------------- #
async def try_mutate_approved() -> dict[str, Any]:
    """Insert an approved action and try to rewrite its arguments behind its back."""

    import sqlalchemy as sa

    settings = load_settings().validate_or_raise()
    database = build_database(settings)
    try:
        async with database.sessions() as session, session.begin():
            await session.execute(
                sa.text(
                    "INSERT INTO pending_actions "
                    "(action_id, user_id, chat_id, action_type, args_json, args_hash, state, "
                    " idempotency_key, expires_at) "
                    "VALUES ('act-trigger-check', 42, 4242, 'send_email_draft', "
                    " '{\"to\": \"a@example.invalid\"}'::jsonb, repeat('a', 64), 'approved', "
                    " 'phase2:trigger', now() + interval '30 minutes')"
                )
            )

        error: dict[str, Any] = {"raised": False}
        try:
            async with database.sessions() as session, session.begin():
                await session.execute(
                    sa.text(
                        "UPDATE pending_actions SET args_json = '{\"to\": \"evil@example.invalid\"}'::jsonb "
                        "WHERE action_id = 'act-trigger-check'"
                    )
                )
        except Exception as exc:  # noqa: BLE001 - the raise is the assertion
            error = {
                "raised": True,
                "type": type(exc).__name__,
                "message": str(exc).splitlines()[0][:200],
            }

        async with database.sessions() as session:
            stored = await session.execute(
                sa.text(
                    "SELECT args_json::text FROM pending_actions WHERE action_id = 'act-trigger-check'"
                )
            )
            error["stored_args"] = stored.scalar_one_or_none()
            active = await session.execute(
                sa.text("SELECT count(*) FROM embedding_models WHERE is_active")
            )
            error["active_embedding_models"] = active.scalar_one()
            trigger = await session.execute(
                sa.text(
                    "SELECT tgname FROM pg_trigger WHERE tgname = 'pending_actions_args_immutable'"
                )
            )
            error["trigger"] = trigger.scalar_one_or_none()
        return error
    finally:
        await database.dispose()


COMMANDS = {
    "seed": seed,
    "read-seed": read_seed,
    "interrupt": interrupt,
    "resume": resume,
    "seed-due": seed_due,
    "try-mutate-approved": try_mutate_approved,
}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in COMMANDS:
        print(f"usage: python -m tests.support.phase2_worker [{'|'.join(COMMANDS)}] [args]")
        return 2

    use_compatible_event_loop()  # Windows: psycopg async needs the selector loop
    command, rest = args[0], args[1:]
    try:
        payload = asyncio.run(COMMANDS[command](*rest))
    except Exception as exc:  # noqa: BLE001 - the worker must report, not traceback
        _emit({"ok": False, "command": command, "error": type(exc).__name__, "detail": str(exc)[:300]})
        return 1
    _emit({"ok": True, "command": command, **payload})
    return 0


if __name__ == "__main__":
    sys.exit(main())
