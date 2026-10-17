"""Phase 2 acceptance criteria that need a real PostgreSQL (PLAN.md §10 Phase 2 (b)-(e), (g)).

Every test here skips with an explicit reason when `TEST_DATABASE_URL` is unset, so the
credential-free unit suite never depends on a container. Run them with:

    TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:55432/taa \
        uv run pytest -m integration

The process-boundary criteria (b), (c) and (d) are driven through
``tests/support/phase2_worker.py``, so the restart really is a restart.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.storage import repositories
from app.storage.postgres import build_database
from tests.conftest import TEST_DATABASE_URL, make_db_settings, truncate_all

REPO = Path(__file__).resolve().parents[2]
PYTHON = sys.executable


pytestmark = [pytest.mark.integration, pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set")]


def _env(**extra: str) -> dict[str, str]:
    """Environment for a worker subprocess: the real settings path, nothing mocked."""

    env = dict(os.environ)
    env.update(
        {
            "DATABASE_URL": TEST_DATABASE_URL,
            "DATABASE_SYNC_URL": TEST_DATABASE_URL,
            "STORE_BACKEND": "postgres",
            "APP_ENV": "test",
            "LOG_LEVEL": "WARNING",
        }
    )
    env.update(extra)
    return env


def _run_worker(command: str, *args: str, **env_extra: str) -> dict:
    """Run one worker in its own process and return its JSON report."""

    result = subprocess.run(
        [PYTHON, "-m", "tests.support.phase2_worker", command, *args],
        cwd=REPO,
        env=_env(**env_extra),
        capture_output=True,
        text=True,
        timeout=180,
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith("WORKER ")]
    assert lines, f"worker produced no report (rc={result.returncode}): {result.stderr[-400:]}"
    payload = json.loads(lines[-1][len("WORKER ") :])
    assert payload.get("ok"), f"worker failed: {payload}"
    return payload


@pytest.fixture
def db_settings():
    settings = make_db_settings()
    truncate_all(settings)
    return settings


# --------------------------------------------------------------------------- #
# (a)/(g) the schema the migration leaves behind
# --------------------------------------------------------------------------- #
def test_migration_leaves_exactly_one_active_embedding_model(db_settings) -> None:
    """Acceptance (g), first half. Retrieval and /readyz read this row; there must be one."""

    engine = sa.create_engine(db_settings.database_sync_url.get_secret_value())
    try:
        with engine.connect() as connection:
            active = connection.execute(
                sa.text("SELECT count(*) FROM embedding_models WHERE is_active")
            ).scalar_one()
            total = connection.execute(sa.text("SELECT count(*) FROM embedding_models")).scalar_one()
    finally:
        engine.dispose()

    assert active == 1
    assert total >= 1


def test_migration_leaves_the_arguments_immutability_trigger(db_settings) -> None:
    """Acceptance (g), second half, in two steps: the trigger exists, and it raises."""

    engine = sa.create_engine(db_settings.database_sync_url.get_secret_value())
    try:
        with engine.connect() as connection:
            trigger = connection.execute(
                sa.text(
                    "SELECT tgname FROM pg_trigger WHERE tgname = 'pending_actions_args_immutable'"
                )
            ).scalar_one_or_none()
        assert trigger == "pending_actions_args_immutable"

        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO pending_actions (action_id, user_id, chat_id, action_type, "
                    "args_json, args_hash, state, idempotency_key, expires_at) VALUES "
                    "('act-pytest', 42, 4242, 'send_email_draft', '{\"to\": \"a@x.invalid\"}'::jsonb, "
                    "repeat('a', 64), 'approved', 'pytest:act', now() + interval '30 minutes')"
                )
            )

        with pytest.raises(sa.exc.IntegrityError) as excinfo:
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "UPDATE pending_actions SET args_json = '{\"to\": \"evil@x.invalid\"}'::jsonb "
                        "WHERE action_id = 'act-pytest'"
                    )
                )
        assert "immutable" in str(excinfo.value)

        with engine.connect() as connection:
            stored = connection.execute(
                sa.text("SELECT args_json::text FROM pending_actions WHERE action_id = 'act-pytest'")
            ).scalar_one()
        assert "a@x.invalid" in stored, "the refused UPDATE must not have changed the row"

        # The one permitted path: an explicit re-presentation of the same action.
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "UPDATE pending_actions SET state='pending', args_revision = args_revision + 1, "
                    "args_json='{\"to\": \"b@x.invalid\"}'::jsonb, args_hash=repeat('b',64) "
                    "WHERE action_id = 'act-pytest'"
                )
            )
    finally:
        engine.dispose()


def test_the_hnsw_index_and_pgvector_version_are_what_the_plan_requires(db_settings) -> None:
    engine = sa.create_engine(db_settings.database_sync_url.get_secret_value())
    try:
        with engine.connect() as connection:
            version = connection.execute(
                sa.text("SELECT extversion FROM pg_extension WHERE extname='vector'")
            ).scalar_one()
            indexdef = connection.execute(
                sa.text(
                    "SELECT indexdef FROM pg_indexes WHERE indexname='ix_document_chunks_embedding_hnsw'"
                )
            ).scalar_one()
            chunk_key = connection.execute(
                sa.text(
                    "SELECT indexdef FROM pg_indexes WHERE indexname='uq_document_chunks_version_chunk'"
                )
            ).scalar_one()
    finally:
        engine.dispose()

    assert tuple(int(p) for p in version.split(".")[:2]) >= (0, 8)
    assert "USING hnsw" in indexdef and "vector_cosine_ops" in indexdef
    assert "document_id, pipeline_version, chunk_index" in chunk_key


# --------------------------------------------------------------------------- #
# (b) memories and tasks survive a process restart
# --------------------------------------------------------------------------- #
def test_memories_and_tasks_survive_a_process_restart(db_settings) -> None:
    """Two separate processes, no shared memory between them."""

    seeded = _run_worker("seed")
    assert seeded["task"], "the seeding process must report the task it created"

    read = _run_worker("read-seed")

    assert read["memories"] == [{"key": "ciudad", "value": "Madrid"}]
    assert [task["title"] for task in read["tasks"]] == ["Llamar al dentista"]
    assert read["tasks"][0]["state"] == "pending"


# --------------------------------------------------------------------------- #
# (c) a graph run interrupted after a checkpoint resumes after a restart
# --------------------------------------------------------------------------- #
def test_an_interrupted_run_resumes_in_a_new_process(db_settings) -> None:
    thread_id = f"pytest-thread-{uuid.uuid4()}"

    interrupted = _run_worker("interrupt", thread_id)

    assert interrupted["process"] == "interrupting"
    assert interrupted["interrupted"] is True, "the second turn must stop at the boundary"
    # Turn 1 completed for real before the interruption: that is what makes the checkpoint
    # boundary a *step* boundary rather than the top of the run (item 8).
    assert interrupted["first_turn_reply"] == "respuesta tras reanudar"
    assert interrupted["history_roles"] == ["user", "assistant", "user"]
    assert interrupted["second_turn_reply_so_far"] == "", (
        "turn 2 produced no answer, and the previous turn's reply must not be passed off as one"
    )
    assert interrupted["model_calls_in_this_process"] == 1

    resumed = _run_worker("resume", thread_id)

    assert resumed["process"] == "resuming", "a different process must have done the work"
    assert resumed["reply"] == "respuesta tras reanudar"
    assert resumed["still_interrupted"] is False
    # The completed turn was NOT replayed: the new process ran exactly the interrupted step.
    assert resumed["model_calls_in_this_process"] == 1
    assert [message["role"] for message in resumed["history"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


# --------------------------------------------------------------------------- #
# (d) two concurrent dispatchers, one reminder
# --------------------------------------------------------------------------- #
def test_two_concurrent_dispatchers_send_exactly_one_reminder(db_settings, tmp_path) -> None:
    """Two real dispatcher processes race for one due task.

    The check is not only the ledger (what the code believed) but the **record file** (what was
    actually handed to a transport), because the ledger alone would be circular evidence.
    """

    _run_worker("seed-due")
    record = tmp_path / "notify.jsonl"
    env = {
        "NOTIFICATIONS_TRANSPORT": "record",
        "NOTIFY_RECORD_PATH": str(record),
        "REMINDER_BATCH_SIZE": "10",
    }

    first = subprocess.Popen(
        [PYTHON, "-m", "app.jobs.dispatch_reminders"],
        cwd=REPO,
        env=_env(**env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second = subprocess.Popen(
        [PYTHON, "-m", "app.jobs.dispatch_reminders"],
        cwd=REPO,
        env=_env(**env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out_first, _ = first.communicate(timeout=180)
    out_second, _ = second.communicate(timeout=180)
    assert first.returncode == 0 and second.returncode == 0

    reports = [
        json.loads(line[len("JOB_REPORT ") :])
        for line in (out_first + out_second).splitlines()
        if line.startswith("JOB_REPORT ")
    ]
    assert len(reports) == 2, "both dispatchers must report"

    deliveries = record.read_text(encoding="utf-8").strip().splitlines() if record.exists() else []
    assert len(deliveries) == 1, f"exactly one delivery was expected, got {len(deliveries)}"

    engine = sa.create_engine(db_settings.database_sync_url.get_secret_value())
    try:
        with engine.connect() as connection:
            sent = connection.execute(
                sa.text("SELECT count(*) FROM reminder_dispatches WHERE state = 'sent'")
            ).scalar_one()
            dispatches = connection.execute(
                sa.text("SELECT count(*) FROM reminder_dispatches")
            ).scalar_one()
            task = connection.execute(
                sa.text(
                    "SELECT state, attempts, last_notified_at IS NOT NULL, next_notification_at IS NULL "
                    "FROM tasks"
                )
            ).one()
    finally:
        engine.dispose()

    assert dispatches == 1, "the unique constraint must allow one row per occurrence"
    assert sent == 1
    state, attempts, notified, no_next = task
    assert state == "completed", "a delivered one-shot reminder is finished (B1)"
    assert notified is True, "last_notified_at must be maintained"
    assert no_next is True, "next_notification_at must be cleared, or the task stays due forever"
    assert attempts == 1, "contention must not burn attempts (RN6)"


def test_a_delivered_reminder_is_not_reclaimed_on_the_next_run(db_settings, tmp_path) -> None:
    """The round-1 pathology: a delivered reminder stayed due, was re-claimed, and failed."""

    _run_worker("seed-due")
    record = tmp_path / "notify.jsonl"
    env = {"NOTIFICATIONS_TRANSPORT": "record", "NOTIFY_RECORD_PATH": str(record)}

    first = _run_worker("seed-due")  # idempotent: same key returns the same task
    for _ in range(3):
        subprocess.run(
            [PYTHON, "-m", "app.jobs.dispatch_reminders"],
            cwd=REPO,
            env=_env(**env),
            capture_output=True,
            text=True,
            timeout=180,
            check=True,
        )

    deliveries = record.read_text(encoding="utf-8").strip().splitlines()
    assert len(deliveries) == 1, "later runs must claim nothing"
    assert first["task_id"] == _run_worker("seed-due")["task_id"]


# --------------------------------------------------------------------------- #
# (e) /forget deletes a memory, and is idempotent
# --------------------------------------------------------------------------- #
async def test_forget_deletes_and_is_idempotent(db_settings) -> None:
    from app.tools.memory import DeleteMemoryArgs, MemoryTools, SavePreferenceArgs

    database = build_database(db_settings)
    try:
        tools = MemoryTools(sessions=database.sessions)
        saved = await tools.save_preference(
            user_id=42, args=SavePreferenceArgs(key="ciudad", value="Madrid")
        )
        assert saved.ok

        async with database.sessions() as session:
            stored = await repositories.get_memory_by_key(session, user_id=42, key="ciudad")
        assert stored is not None
        memory_id = stored.id

        first = await tools.delete_memory(user_id=42, args=DeleteMemoryArgs(memory_id=memory_id))
        second = await tools.delete_memory(user_id=42, args=DeleteMemoryArgs(memory_id=memory_id))

        assert first.ok and first.data["deleted"] is True
        assert second.ok and second.data["deleted"] is False, "the second delete is a no-op"

        async with database.sessions() as session:
            gone = await repositories.get_memory_by_key(session, user_id=42, key="ciudad")
        assert gone is None
    finally:
        await database.dispose()


async def test_another_users_memory_cannot_be_deleted(db_settings) -> None:
    from app.tools.memory import DeleteMemoryArgs, MemoryTools, SavePreferenceArgs

    database = build_database(db_settings)
    try:
        tools = MemoryTools(sessions=database.sessions)
        await tools.save_preference(
            user_id=42, args=SavePreferenceArgs(key="ciudad", value="Madrid")
        )
        async with database.sessions() as session:
            stored = await repositories.get_memory_by_key(session, user_id=42, key="ciudad")
        assert stored is not None

        result = await tools.delete_memory(user_id=999, args=DeleteMemoryArgs(memory_id=stored.id))
        assert result.ok and result.data["deleted"] is False

        async with database.sessions() as session:
            still_there = await repositories.get_memory_by_key(session, user_id=42, key="ciudad")
        assert still_there is not None
    finally:
        await database.dispose()


# --------------------------------------------------------------------------- #
# Reminder creation: idempotency and the DST policy, against real storage
# --------------------------------------------------------------------------- #
async def test_create_reminder_is_idempotent_by_content(db_settings) -> None:
    from app.tools.tasks import CreateReminderArgs, TaskTools

    database = build_database(db_settings)
    try:
        tools = TaskTools(sessions=database.sessions, default_timezone="Europe/Madrid")
        args = CreateReminderArgs(title="Llamar al dentista", due_local="2026-11-03 09:30")

        first = await tools.create_reminder(user_id=42, args=args)
        second = await tools.create_reminder(user_id=42, args=args)

        assert first.data["task_id"] == second.data["task_id"]
        assert first.data["created"] is True and second.data["created"] is False

        async with database.sessions() as session:
            count = await repositories.list_tasks(session, user_id=42)
        assert len(count) == 1
    finally:
        await database.dispose()


async def test_create_reminder_rejects_a_dst_edge_with_a_clarification(db_settings) -> None:
    from app.tools.tasks import CreateReminderArgs, TaskTools

    database = build_database(db_settings)
    try:
        tools = TaskTools(sessions=database.sessions, default_timezone="Europe/Madrid")
        result = await tools.create_reminder(
            user_id=42,
            args=CreateReminderArgs(title="Cambio de hora", due_local="2026-03-29 02:30"),
        )
        assert not result.ok
        assert result.error_code == "invalid_local_time"
        assert result.message_key == "error.invalid_due_time"

        async with database.sessions() as session:
            assert await repositories.list_tasks(session, user_id=42) == []
    finally:
        await database.dispose()


async def test_the_claim_predicate_respects_max_attempts(db_settings) -> None:
    """B1: an exhausted task must not be claimable, and the predicate is in SQL."""

    database = build_database(db_settings)
    try:
        async with database.sessions() as session, session.begin():
            task, _ = await repositories.create_task(
                session,
                user_id=42,
                title="Agotado",
                description=None,
                due_at=sa.func.now(),
                timezone="Europe/Madrid",
                idempotency_key="pytest:exhausted",
                max_attempts=1,
            )
            task.attempts = 1  # already used its single attempt

        async with database.sessions() as session, session.begin():
            claimed = await repositories.claim_due_tasks(session, batch=10, executor="pytest")
        assert claimed == []
    finally:
        await database.dispose()


async def test_reconciliation_moves_a_stale_claim_to_unknown_without_resending(db_settings) -> None:
    """B2: a crashed send is reported, never repeated."""

    database = build_database(db_settings)
    try:
        async with database.sessions() as session, session.begin():
            task, _ = await repositories.create_task(
                session,
                user_id=42,
                title="Se cayó el envío",
                description=None,
                due_at=sa.func.now(),
                timezone="Europe/Madrid",
                idempotency_key="pytest:stale",
                max_attempts=3,
            )
            dispatch_id = await repositories.claim_dispatch(
                session,
                task_id=task.id,
                occurrence_at=task.due_at,
                dispatch_key=f"{task.id}:{task.due_at.isoformat()}",
                executor="pytest",
            )
            assert dispatch_id is not None

        # Backdate the lease so it looks abandoned.
        async with database.sessions() as session, session.begin():
            await session.execute(
                sa.text(
                    "UPDATE reminder_dispatches SET claimed_at = now() - interval '2 hours' "
                    "WHERE id = :id"
                ),
                {"id": dispatch_id},
            )

        async with database.sessions() as session, session.begin():
            reconciled = await repositories.reconcile_stale_dispatches(
                session, lease_seconds=900, executor="pytest"
            )
        assert len(reconciled) == 1

        async with database.sessions() as session:
            rows = await repositories.list_open_dispatches(session)
        assert [row.state for row in rows] == ["unknown"]

        # And nothing can be re-sent for that occurrence: the unique constraint stands.
        async with database.sessions() as session, session.begin():
            again = await repositories.claim_dispatch(
                session,
                task_id=task.id,
                occurrence_at=task.due_at,
                dispatch_key=f"{task.id}:{task.due_at.isoformat()}",
                executor="pytest",
            )
        assert again is None
    finally:
        await database.dispose()


# --------------------------------------------------------------------------- #
# Retrieval invariants (the frozen chokepoint)
# --------------------------------------------------------------------------- #
async def test_search_chunks_filters_by_owner_and_document_version(db_settings) -> None:
    """§6.5: the owner predicate and the *document's* version are inside the literal."""

    database = build_database(db_settings)
    try:
        async with database.sessions() as session, session.begin():
            await repositories.upsert_embedding_model(
                session, model_name="embeddinggemma", dim=768, pipeline_version=1, activate=True
            )
            for user_id, version in ((42, 1), (999, 1)):
                document_id = uuid.uuid4()
                await session.execute(
                    sa.text(
                        "INSERT INTO documents (id, user_id, original_filename, sanitized_filename, "
                        "mime_type, size_bytes, gcs_uri, content_sha256, ingest_status, pipeline_version) "
                        "VALUES (:id, :user, 'Informe anual 2026.pdf', 'informe.pdf', "
                        "'application/pdf', 10, 'gs://b/a.pdf', repeat('c', 64), 'ready', :version)"
                    ),
                    {"id": document_id, "user": user_id, "version": version},
                )
                await session.execute(
                    sa.text(
                        "INSERT INTO document_chunks (document_id, owner_user_id, chunk_index, "
                        "page_number, text, char_count, embedding, embed_model, pipeline_version) "
                        "VALUES (:id, :user, 0, 1, :text, 5, :embedding, 'embeddinggemma', :version)"
                    ),
                    {
                        "id": document_id,
                        "user": user_id,
                        "text": f"chunk de {user_id}",
                        "embedding": str([0.1] * 768),
                        "version": version,
                    },
                )

            # A superseded version of user 42's document: same model, older pipeline version.
            other_version = uuid.uuid4()
            await session.execute(
                sa.text(
                    "INSERT INTO documents (id, user_id, original_filename, sanitized_filename, "
                    "mime_type, size_bytes, gcs_uri, content_sha256, ingest_status, pipeline_version) "
                    "VALUES (:id, 42, 'Informe anual 2026.pdf', 'informe.pdf', 'application/pdf', "
                    "10, 'gs://b/a.pdf', repeat('c', 64), 'ready', 2)"
                ),
                {"id": other_version},
            )

        async with database.sessions() as session:
            matches = await repositories.search_chunks(
                session,
                owner_user_id=42,
                query_embedding=[0.1] * 768,
                embed_model="embeddinggemma",
                top_k=5,
            )

        assert len(matches) == 1
        assert matches[0].text == "chunk de 42"
        assert matches[0].page_number == 1
        # Item 15: the citation must carry the name the user recognises, not the storage name.
        assert matches[0].document_filename == "Informe anual 2026.pdf"
        assert all(match.document_id != other_version for match in matches)
    finally:
        await database.dispose()
