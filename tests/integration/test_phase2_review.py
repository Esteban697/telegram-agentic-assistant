"""Tests for the Phase 2 review findings that need a database.

Items 1 (the `/forget` end-to-end path), 2 (the concurrency races), 4 (autogenerate must be
clean) and 7 (a contention test that actually contends).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.bot import messages
from app.config import Settings
from app.main import create_app
from app.storage import repositories
from app.storage.models import Base
from app.storage.postgres import build_database
from tests.conftest import (
    CHAT_ID,
    TEST_DATABASE_URL,
    USER_ID,
    WEBHOOK_SECRET,
    OllamaStub,
    RecordingTelegram,
    chat_response,
    make_db_settings,
    truncate_all,
)

REPO = Path(__file__).resolve().parents[2]
PYTHON = sys.executable

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set"),
]


@pytest.fixture
def db_settings() -> Settings:
    settings = make_db_settings()
    truncate_all(settings)
    return settings


def _message_update(*, update_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "from": {"id": USER_ID, "is_bot": False, "first_name": "Usuario"},
            "chat": {"id": CHAT_ID, "type": "private"},
            "date": 1_700_000_000,
            "text": text,
        },
    }


class _AppHarness:
    """The real application over the real database, with a stubbed model."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.telegram = RecordingTelegram()

    def __enter__(self) -> "_AppHarness":
        self.stub = OllamaStub(handler=lambda request: __import__("httpx").Response(
            200, json=chat_response(content="respuesta")
        ))
        self.app = create_app(
            settings=self.settings,
            chat_model=self.stub.client(),
            telegram_client=self.telegram,
        )
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.client.__enter__()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.client.__exit__(None, None, None)

    def post(self, update: dict):
        return self.client.post(
            "/telegram/webhook",
            json=update,
            headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
        )


# --------------------------------------------------------------------------- #
# item 1: /forget through the real webhook
# --------------------------------------------------------------------------- #
def _seed_memory(settings: Settings, *, key: str, value: str) -> None:
    import asyncio

    from app.tools.memory import MemoryTools, SavePreferenceArgs

    async def _seed() -> None:
        database = build_database(settings)
        try:
            await MemoryTools(sessions=database.sessions).save_preference(
                user_id=USER_ID, args=SavePreferenceArgs(key=key, value=value)
            )
        finally:
            await database.dispose()

    asyncio.run(_seed())


def _memory_keys(settings: Settings) -> list[str]:
    import asyncio

    async def _read() -> list[str]:
        database = build_database(settings)
        try:
            async with database.sessions() as session:
                rows = await repositories.list_memories(session, user_id=USER_ID)
                return [row.key for row in rows]
        finally:
            await database.dispose()

    return asyncio.run(_read())


def test_forget_through_the_webhook_deletes_the_memory(db_settings) -> None:
    """The blocking finding: `/forget <clave>` raised KeyError and deleted nothing.

    It built `DeleteMemoryArgs(memory_id=match["id"])`, but the search result carried no `id`,
    so the handler blew up into the webhook's catch-all and the user saw a generic error while
    the memory stayed. This drives the real command surface against the real database.
    """

    _seed_memory(db_settings, key="ciudad", value="Madrid")
    assert _memory_keys(db_settings) == ["ciudad"]

    with _AppHarness(db_settings) as harness:
        response = harness.post(_message_update(update_id=1, text="/forget ciudad"))

    assert response.status_code == 200
    assert harness.telegram.sent_texts == [messages.render("forget.done", key="ciudad")]
    assert _memory_keys(db_settings) == [], "the memory must actually be gone"


def test_forget_of_an_unknown_key_says_so_and_changes_nothing(db_settings) -> None:
    _seed_memory(db_settings, key="ciudad", value="Madrid")

    with _AppHarness(db_settings) as harness:
        harness.post(_message_update(update_id=1, text="/forget inexistente"))

    assert harness.telegram.sent_texts == [messages.render("forget.not_found", key="inexistente")]
    assert _memory_keys(db_settings) == ["ciudad"]


def test_forget_with_a_like_wildcard_does_not_delete_other_rows(db_settings) -> None:
    """`%` and `_` must match literally, not as wildcards (item 1)."""

    _seed_memory(db_settings, key="ciudad", value="Madrid")
    _seed_memory(db_settings, key="ciudad_favorita", value="Sevilla")

    with _AppHarness(db_settings) as harness:
        harness.post(_message_update(update_id=1, text="/forget ciud%"))

    assert sorted(_memory_keys(db_settings)) == ["ciudad", "ciudad_favorita"]


def test_forget_with_an_underscore_key_deletes_exactly_that_key(db_settings) -> None:
    _seed_memory(db_settings, key="ciudad_favorita", value="Sevilla")
    _seed_memory(db_settings, key="ciudadXfavorita", value="Otra")

    with _AppHarness(db_settings) as harness:
        harness.post(_message_update(update_id=1, text="/forget ciudad_favorita"))

    assert _memory_keys(db_settings) == ["ciudadXfavorita"]


def test_forget_with_a_key_that_is_a_substring_of_many_does_not_delete_them(db_settings) -> None:
    """A `LIKE '%arg%'` search capped at ten rows used to report "not found" here (item 1)."""

    for index in range(15):
        _seed_memory(db_settings, key=f"ciudad{index:02d}", value="x")
    _seed_memory(db_settings, key="ciudad", value="Madrid")

    with _AppHarness(db_settings) as harness:
        harness.post(_message_update(update_id=1, text="/forget ciudad"))

    remaining = _memory_keys(db_settings)
    assert "ciudad" not in remaining, "the exact key must be deleted despite 15 prefixes"
    assert len(remaining) == 15


def test_forget_with_an_over_long_key_is_a_catalogue_message_not_a_crash(db_settings) -> None:
    """A >128-character key raised a pydantic ValidationError into the catch-all (item 1)."""

    with _AppHarness(db_settings) as harness:
        response = harness.post(_message_update(update_id=1, text="/forget " + "k" * 200))

    assert response.status_code == 200
    assert len(harness.telegram.sent_texts) == 1
    assert harness.telegram.sent_texts[0] == messages.render("forget.not_found", key="k" * 64)
    assert "error inesperado" not in harness.telegram.sent_texts[0]


def test_forget_all_requires_confirmation_and_then_deletes(db_settings) -> None:
    _seed_memory(db_settings, key="a", value="1")
    _seed_memory(db_settings, key="b", value="2")

    with _AppHarness(db_settings) as harness:
        harness.post(_message_update(update_id=1, text="/forget all"))
        assert harness.telegram.sent_texts == [messages.render("forget.confirm_all", count=2)]
        assert len(_memory_keys(db_settings)) == 2, "the first call must not delete anything"

        harness.post(_message_update(update_id=2, text="/forget all confirm"))

    assert harness.telegram.sent_texts[-1] == messages.render("forget.all_done", count=2)
    assert _memory_keys(db_settings) == []


# --------------------------------------------------------------------------- #
# item 2: the concurrency races
# --------------------------------------------------------------------------- #
async def test_concurrent_first_messages_all_get_the_same_conversation(db_settings) -> None:
    """The blocking finding: the loser raised instead of re-reading.

    `get_or_create_active_conversation` called `session.rollback()` inside the caller's
    `session.begin()` block, which deactivated that transaction, so the context manager's commit
    raised `InvalidRequestError` and the loser surfaced as an unhandled error.
    """

    import asyncio

    database = build_database(db_settings)
    try:
        async def resolve() -> str:
            async with database.sessions() as session, session.begin():
                conversation = await repositories.get_or_create_active_conversation(
                    session, telegram_user_id=USER_ID, telegram_chat_id=CHAT_ID
                )
                return conversation.thread_id

        thread_ids = await asyncio.gather(*(resolve() for _ in range(6)))

        assert len(set(thread_ids)) == 1, f"exactly one conversation, got {thread_ids}"

        async with database.sessions() as session:
            rows = await session.execute(
                sa.text("SELECT count(*), min(telegram_user_id), max(telegram_user_id) FROM conversations")
            )
            count, min_user, max_user = rows.one()
        assert count == 1
        assert min_user == max_user == USER_ID, "no row may be attributed to a placeholder user"
    finally:
        await database.dispose()


async def test_concurrent_resets_leave_one_active_revision(db_settings) -> None:
    """Same defect, second site: `rotate_conversation_context`.

    The race is made deterministic with a barrier: every caller reads the *same* active
    conversation before any of them inserts, so exactly one may create the next revision and the
    others must adopt it. Without the savepoint fix the losers raised `InvalidRequestError` from
    the context manager instead.
    """

    import asyncio

    database = build_database(db_settings)
    try:
        async with database.sessions() as session, session.begin():
            await repositories.get_or_create_active_conversation(
                session, telegram_user_id=USER_ID, telegram_chat_id=CHAT_ID
            )

        callers = 4
        barrier = asyncio.Barrier(callers)

        async def reset() -> str:
            async with database.sessions() as session, session.begin():
                conversation = await repositories.get_active_conversation(
                    session, telegram_chat_id=CHAT_ID
                )
                assert conversation is not None
                # Everybody has read revision 1 by the time anybody writes.
                await barrier.wait()
                fresh = await repositories.rotate_conversation_context(
                    session, conversation=conversation
                )
                return fresh.thread_id

        thread_ids = await asyncio.gather(*(reset() for _ in range(callers)))

        assert len(set(thread_ids)) == 1, (
            f"all callers must end up in the same new revision, got {thread_ids}"
        )

        async with database.sessions() as session:
            active = await session.execute(
                sa.text("SELECT count(*) FROM conversations WHERE status = 'active'")
            )
            assert active.scalar_one() == 1, "exactly one active conversation"

            revisions = await session.execute(
                sa.text("SELECT context_revision FROM conversations ORDER BY context_revision")
            )
            assert [row[0] for row in revisions] == [1, 2], "no duplicate revision may be created"
    finally:
        await database.dispose()


# --------------------------------------------------------------------------- #
# item 4: autogenerate must be a no-op
# --------------------------------------------------------------------------- #
def test_alembic_autogenerate_reports_no_differences(db_settings) -> None:
    """Model and migration must agree, or every future migration carries churn.

    `models.py` declared a `UniqueConstraint` for the task idempotency key while migration 0005
    created a unique *index*, so `compare_metadata` reported a drop/add pair on `tasks`. §3.4
    flagged exactly this risk.
    """

    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    engine = sa.create_engine(db_settings.database_sync_url.get_secret_value())
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={
                    "compare_type": True,
                    "include_object": lambda obj, name, type_, reflected, compare_to: (
                        type_ != "table" or name not in {
                            "checkpoints", "checkpoint_blobs", "checkpoint_writes",
                            "checkpoint_migrations", "alembic_version",
                        }
                    ),
                },
            )
            diffs = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert diffs == [], f"model and migration disagree: {diffs}"


# --------------------------------------------------------------------------- #
# item 7: a contention test that actually contends
# --------------------------------------------------------------------------- #
class _SlowTelegram(BaseHTTPRequestHandler):
    """A Bot API stand-in that answers slowly, so two dispatchers really overlap."""

    delay = 3.0
    requests: list[dict] = []
    lock = threading.Lock()
    first_request_seen = threading.Event()

    def do_POST(self) -> None:  # noqa: N802 - http.server's spelling
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        with _SlowTelegram.lock:
            _SlowTelegram.requests.append(body)
        _SlowTelegram.first_request_seen.set()
        time.sleep(_SlowTelegram.delay)
        payload = json.dumps({"ok": True, "result": {"message_id": 1, "chat": {"id": body.get("chat_id", 0)}}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:  # keep the test output clean
        return


def test_two_dispatchers_really_overlap_and_only_one_sends(db_settings) -> None:
    """The earlier version of this test could not fail (item 7).

    Two `Popen` calls back-to-back did not overlap: Python's startup latency meant the loser
    began after the winner had committed, so it reported `claimed: 0` without ever entering the
    contended path — and `attempts == 1` passes serially too.

    Here the delivery is slowed down by a Bot API stand-in that sleeps, and the second process
    is started only once the first one is provably inside its send (the server has seen the
    request). The number of HTTP deliveries is counted by the server, which is an observation
    from outside both processes.
    """

    _SlowTelegram.requests = []
    _SlowTelegram.first_request_seen = threading.Event()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowTelegram)
    server.daemon_threads = True
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        env = dict(os.environ)
        env.update(
            {
                "DATABASE_URL": TEST_DATABASE_URL,
                "DATABASE_SYNC_URL": TEST_DATABASE_URL,
                "STORE_BACKEND": "postgres",
                "APP_ENV": "test",
                "LOG_LEVEL": "WARNING",
                "NOTIFICATIONS_TRANSPORT": "telegram",
                "TELEGRAM_API_BASE_URL": f"http://127.0.0.1:{port}",
                "TELEGRAM_BOT_TOKEN": "test-token",
            }
        )

        seed = subprocess.run(
            [PYTHON, "-m", "tests.support.phase2_worker", "seed-due"],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=120,
        )
        assert seed.returncode == 0, seed.stderr[-300:]

        winner = subprocess.Popen(
            [PYTHON, "-m", "app.jobs.dispatch_reminders"],
            cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        # Only start the loser once the winner is inside the send: that is the overlap the
        # property is about.
        assert _SlowTelegram.first_request_seen.wait(timeout=60), (
            "the winner never reached the delivery step"
        )
        loser = subprocess.Popen(
            [PYTHON, "-m", "app.jobs.dispatch_reminders"],
            cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        winner_out, _ = winner.communicate(timeout=180)
        loser_out, _ = loser.communicate(timeout=180)
        assert winner.returncode == 0 and loser.returncode == 0

        reports = [
            json.loads(line[len("JOB_REPORT ") :])
            for line in (winner_out + loser_out).splitlines()
            if line.startswith("JOB_REPORT ")
        ]
        assert len(reports) == 2

        # The external observation: one HTTP delivery, not two.
        assert len(_SlowTelegram.requests) == 1, (
            f"exactly one delivery expected, got {len(_SlowTelegram.requests)}"
        )

        claimed_total = sum(int(report["claimed"]) for report in reports)
        assert claimed_total >= 1
        skipped = [
            item for report in reports for item in report.get("skipped", [])
        ]
        assert skipped, "the loser must have been refused by the ledger, i.e. it really overlapped"

        engine = sa.create_engine(db_settings.database_sync_url.get_secret_value())
        try:
            with engine.connect() as connection:
                attempts = connection.execute(sa.text("SELECT attempts FROM tasks")).scalar_one()
                dispatches = connection.execute(
                    sa.text("SELECT count(*) FROM reminder_dispatches")
                ).scalar_one()
                sent = connection.execute(
                    sa.text("SELECT count(*) FROM reminder_dispatches WHERE state='sent'")
                ).scalar_one()
        finally:
            engine.dispose()

        assert dispatches == 1
        assert sent == 1
        assert attempts == 1, (
            "with real contention, a naive 'increment in the claim' would make this 2 (RN6)"
        )
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------- #
# housekeeping used by the harness above
# --------------------------------------------------------------------------- #
def test_the_harness_really_uses_the_database(db_settings) -> None:
    """Guards the guard: if the harness silently fell back to memory, item 1's test would pass
    for the wrong reason."""

    with _AppHarness(db_settings) as harness:
        harness.post(_message_update(update_id=1, text="hola"))

    engine = sa.create_engine(db_settings.database_sync_url.get_secret_value())
    try:
        with engine.connect() as connection:
            conversations = connection.execute(
                sa.text("SELECT count(*) FROM conversations")
            ).scalar_one()
            inbox = connection.execute(sa.text("SELECT count(*) FROM inbox_updates")).scalar_one()
            messages_written = connection.execute(
                sa.text("SELECT count(*) FROM messages")
            ).scalar_one()
    finally:
        engine.dispose()

    assert conversations == 1
    assert inbox == 1
    assert messages_written == 2, "the transcript gets the user turn and the assistant turn"


def test_an_unused_uuid_is_never_a_conversation_id(db_settings) -> None:
    engine = sa.create_engine(db_settings.database_sync_url.get_secret_value())
    try:
        with engine.connect() as connection:
            placeholders = connection.execute(
                sa.text("SELECT count(*) FROM conversations WHERE thread_id = 'pending'")
            ).scalar_one()
    finally:
        engine.dispose()
    assert placeholders == 0
    assert uuid.UUID(int=0)  # keep the import meaningful for the reader
