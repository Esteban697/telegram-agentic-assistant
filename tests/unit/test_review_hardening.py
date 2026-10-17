"""Review hardening: redaction depth, provider-body leaks, retry arithmetic, dev warnings,
boolean update ids, and the timing-safe comparison.

Each test here corresponds to a finding from the Phase 1 adversarial review, and each one
fails against the code as it stood before the fix.
"""

from __future__ import annotations

import hmac
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.telegram_webhook import _secret_matches
from app.config import Settings
from app.logging_config import JsonFormatter, redact
from app.errors import AssistantError
from app.main import create_app
from app.policy.limits import backoff_seconds, total_retry_backoff, worst_case_provider_seconds
from app.storage.stores import GraphConversationStore
from tests.conftest import OllamaStub, RecordingTelegram, chat_response, make_settings

SECRET = "s3cret_token-ABC"


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1, msg="m", args=(), exc_info=None
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


# --------------------------------------------------------------------------- #
# 1a. Redaction must reach nested structures
# --------------------------------------------------------------------------- #
def test_nested_blocked_keys_are_redacted() -> None:
    """The reviewer's finding: only top-level keys were inspected.

    ``extra={"meta": {"content": "..."}}`` carried a user message into the log because
    ``content`` was nested one level down.
    """

    formatter = JsonFormatter(app_env="test")
    payload = json.loads(
        formatter.format(
            _record(
                meta={"content": "MENSAJE PRIVADO", "token": "secret-token", "safe": "ok"},
                items=[{"text": "OTRO TEXTO PRIVADO"}, {"wrapper": {"body": "TERCERO"}}],
                payload={"nested": {"deep": "CUARTO"}},
            )
        )
    )

    assert payload["meta"]["content"] == "[redacted]"
    assert payload["meta"]["token"] == "[redacted]"
    assert payload["meta"]["safe"] == "ok", "unblocked keys must survive"
    assert payload["items"][0]["text"] == "[redacted]"
    # A blocked key suppresses its whole subtree, however deep.
    assert payload["items"][1]["wrapper"]["body"] == "[redacted]"
    assert payload["payload"] == "[redacted]", "a blocked parent blocks its descendants"

    rendered = json.dumps(payload)
    for leak in ("MENSAJE PRIVADO", "OTRO TEXTO PRIVADO", "TERCERO", "CUARTO", "secret-token"):
        assert leak not in rendered, f"{leak} leaked into the log line"


def test_redaction_is_bounded_in_depth_and_breadth() -> None:
    deep: dict = {"a": {"b": {"c": {"d": {"e": {"f": "bottom"}}}}}}
    result = redact(deep)
    assert "bottom" not in json.dumps(result), "depth must be bounded"

    wide = redact({f"k{i}": i for i in range(500)})
    assert len(wide) <= 50, "breadth must be bounded"


# --------------------------------------------------------------------------- #
# 1b. A provider error body must never reach the log
# --------------------------------------------------------------------------- #
def test_provider_error_body_never_reaches_the_log(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end: the provider echoes the user's message in a 400 body.

    Before the fix, ``_safe_error_detail`` returned that body and it was logged as
    ``error_detail`` (PLAN.md §7.4: message content is never logged). Asserted twice — over the
    captured records and over the **real emitted log line** — because a capture that silently
    sees nothing would make this test pass for the wrong reason.
    """

    echoed = "MENSAJE-PRIVADO-QUE-EL-PROVEEDOR-REPITE"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": "invalid request",
                "request_echo": {"messages": [{"content": echoed}]},
            },
        )

    stub = OllamaStub(handler=handler)
    settings = make_settings()
    telegram = RecordingTelegram()
    app = create_app(
        settings=settings, chat_model=stub.client(), telegram_client=telegram
    )

    with caplog.at_level(logging.WARNING):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/telegram/webhook",
                json={
                    "update_id": 1,
                    "message": {
                        "message_id": 1,
                        "from": {"id": 42},
                        "chat": {"id": 4242},
                        "text": "hola",
                    },
                },
                headers={"X-Telegram-Bot-Api-Secret-Token": settings.webhook_secret},
            )

    assert response.status_code == 200

    captured = capsys.readouterr().out
    records = "\n".join(str(record.__dict__) for record in caplog.records)
    assert "model call failed" in captured or "handler failed" in captured, (
        "the failure must actually have been logged — otherwise this test proves nothing"
    )
    assert caplog.records, "capture must not be empty"

    for haystack in (captured, records):
        assert echoed not in haystack, "the provider's echoed body must never be logged"
        assert "request_echo" not in haystack


def test_provider_error_detail_reports_size_not_content() -> None:
    from app.llm.ollama_client import _safe_error_detail

    response = httpx.Response(400, content=b"MENSAJE PRIVADO DEL USUARIO")
    detail = _safe_error_detail(response)
    assert "MENSAJE" not in detail
    assert "usuario" not in detail.lower()
    assert detail == "status=400 body_bytes=27"


# --------------------------------------------------------------------------- #
# 2. The comparison must be timing-safe, not merely correct
# --------------------------------------------------------------------------- #
def test_secret_comparison_uses_hmac_compare_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replacing ``compare_digest`` with ``==`` must fail this test, not just still pass.

    Before the fix the suite asserted only the boolean outcome, so a constant-time comparison
    was indistinguishable from a plain equality check.
    """

    calls: list[tuple[object, object]] = []
    real = hmac.compare_digest

    def spy(a, b):  # noqa: ANN001, ANN202
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)

    assert _secret_matches(SECRET, SECRET) is True
    assert calls, "hmac.compare_digest must be the comparison actually used"
    assert len(calls) == 1

    # Bytes on both sides: comparing `str` to `str` raises TypeError on non-ASCII input,
    # which is exactly the N11 path, so it must never happen.
    left, right = calls[0]
    assert isinstance(left, bytes) and isinstance(right, bytes)


def test_secret_comparison_is_not_short_circuiting_on_missing_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing header must be denied without invoking the comparison at all."""

    def exploding_spy(a, b):  # noqa: ANN001, ANN202
        raise AssertionError("compare_digest must not be called without input")

    monkeypatch.setattr(hmac, "compare_digest", exploding_spy)
    assert _secret_matches(None, SECRET) is False
    assert _secret_matches("", SECRET) is False


# --------------------------------------------------------------------------- #
# 3. The loud development warnings of §7.2
# --------------------------------------------------------------------------- #
def test_empty_allow_list_in_development_warns() -> None:
    settings = make_settings(telegram_allowed_user_ids=frozenset())
    warnings = settings.access_warnings()
    assert any("every user is DENIED" in warning for warning in warnings), warnings


def test_dev_allow_any_user_warns_about_admitting_everybody() -> None:
    settings = make_settings(telegram_allowed_user_ids=frozenset(), dev_allow_any_user=True)
    warnings = settings.access_warnings()
    assert any("ANY Telegram user" in warning for warning in warnings), warnings
    assert not any("every user is DENIED" in warning for warning in warnings), (
        "the two situations are different and must not be reported as the same"
    )


def test_a_configured_development_box_warns_about_nothing_by_default() -> None:
    assert make_settings().access_warnings() == []


def test_production_never_produces_access_warnings() -> None:
    settings = make_settings(
        app_env="production",
        store_backend="postgres",
        telegram_allowed_user_ids=frozenset({1}),
        gcs_bucket="b",
        google_cloud_project="p",
    )
    assert settings.access_warnings() == []


def test_startup_emits_the_warnings(caplog: pytest.LogCaptureFixture) -> None:
    settings = make_settings(telegram_allowed_user_ids=frozenset())
    with caplog.at_level(logging.WARNING):
        create_app(settings=settings, chat_model=OllamaStub(handler=_ok).client(),
                   telegram_client=RecordingTelegram())
    messages = [record.getMessage() for record in caplog.records]
    assert any("every user is DENIED" in message for message in messages), messages


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=chat_response())


# --------------------------------------------------------------------------- #
# 4. The retry budget arithmetic
# --------------------------------------------------------------------------- #
def test_worst_case_accounts_for_retries_and_backoff() -> None:
    assert worst_case_provider_seconds(timeout=10.0, max_retries=0) == pytest.approx(10.0)
    assert worst_case_provider_seconds(timeout=10.0, max_retries=1) == pytest.approx(20.25)
    assert worst_case_provider_seconds(timeout=10.0, max_retries=2) == pytest.approx(30.75)
    assert total_retry_backoff(2) == pytest.approx(backoff_seconds(1) + backoff_seconds(2))


def test_shipped_defaults_keep_the_provider_timeout_reachable() -> None:
    """The default configuration must satisfy its own invariant."""

    settings = Settings()  # defaults only
    assert settings.problems() == [], (
        "the shipped defaults must not violate the budget invariant: "
        f"{settings.problems()}"
    )
    assert (
        worst_case_provider_seconds(
            timeout=settings.llm_timeout_seconds, max_retries=settings.llm_max_retries
        )
        < settings.request_budget_seconds
    )


def test_a_retry_schedule_that_outlasts_the_budget_is_rejected() -> None:
    """30 s x 3 attempts = 90 s against a 45 s budget: the pre-review default, and broken."""

    with pytest.raises(Exception) as excinfo:
        make_settings(llm_timeout_seconds=30.0, llm_max_retries=2, request_budget_seconds=45.0)
    message = str(excinfo.value)
    assert "provider call can outlast the request budget" in message
    assert "90.75" in message, "the message must show the arithmetic"


def test_a_single_timeout_over_the_budget_is_still_rejected() -> None:
    with pytest.raises(Exception):
        make_settings(llm_timeout_seconds=60.0, llm_max_retries=0, request_budget_seconds=45.0)


def test_retries_are_affordable_within_a_tight_budget() -> None:
    # 4.0 x (1 + 1) + 0.25 + 2.0 margin = 10.25 <= 12
    settings = make_settings(
        llm_timeout_seconds=4.0, llm_max_retries=1, request_budget_seconds=12.0
    )
    assert settings.llm_max_retries == 1


# --------------------------------------------------------------------------- #
# 7. `update_id` must not accept a boolean
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_update_id", [True, False])
def test_boolean_update_id_is_rejected(bad_update_id: bool) -> None:
    """``isinstance(True, int)`` is true, so ``false`` used to collide with update id 0."""

    stub = OllamaStub(handler=_ok)
    settings = make_settings()
    telegram = RecordingTelegram()
    app = create_app(settings=settings, chat_model=stub.client(), telegram_client=telegram)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/telegram/webhook",
            json={
                "update_id": bad_update_id,
                "message": {
                    "message_id": 1,
                    "from": {"id": 42},
                    "chat": {"id": 4242},
                    "text": "hola",
                },
            },
            headers={"X-Telegram-Bot-Api-Secret-Token": settings.webhook_secret},
        )
        ledger_entry_for_zero = app.state.dedupe._entries.get(0)  # noqa: SLF001

    assert response.status_code == 200
    assert stub.chat_bodies == [], "a boolean update id must not start a turn"
    assert ledger_entry_for_zero is None, "it must not be recorded as update id 0"


# --------------------------------------------------------------------------- #
# 8. `http://` must be refused outside development
# --------------------------------------------------------------------------- #
def _production(**overrides: object) -> object:
    base = {
        "app_env": "production",
        "store_backend": "postgres",
        "telegram_allowed_user_ids": frozenset({1}),
        "gcs_bucket": "b",
        "google_cloud_project": "p",
    }
    base.update(overrides)
    return make_settings(**base)


def test_plain_http_ollama_url_is_rejected_in_production() -> None:
    with pytest.raises(Exception) as excinfo:
        _production(ollama_base_url="http://ollama.internal:11434")
    assert "must use https:// outside development" in str(excinfo.value)


def test_plain_http_ollama_url_is_rejected_in_test_env() -> None:
    with pytest.raises(Exception) as excinfo:
        make_settings(app_env="test", ollama_base_url="http://127.0.0.1:11434")
    assert "must use https:// outside development" in str(excinfo.value)


def test_plain_http_is_allowed_in_development_only() -> None:
    settings = make_settings(app_env="development", ollama_base_url="http://127.0.0.1:11434")
    assert settings.problems() == []


# --------------------------------------------------------------------------- #
# Phase 2 review, items 11-13
# --------------------------------------------------------------------------- #
def test_exception_messages_survive_into_the_log() -> None:
    """Item 11: the message must be visible, or a bare `KeyError` cannot be diagnosed.

    `exc_message` used `redact(..., key="content")`, which replaced *every* exception message
    with "[redacted]" — exactly why the `/forget` KeyError surfaced with nothing to read.
    """

    import sys

    formatter = JsonFormatter(app_env="test")
    try:
        raise KeyError("id")
    except KeyError:
        record = logging.LogRecord(
            name="t", level=logging.ERROR, pathname=__file__, lineno=1, msg="m", args=(),
            exc_info=sys.exc_info(),
        )
    payload = json.loads(formatter.format(record))
    assert payload["exc_type"] == "KeyError"
    assert "id" in payload["exc_message"], "the diagnostic must survive"
    assert payload["exc_message"] != "[redacted]"


def test_a_credential_inside_an_exception_message_is_still_scrubbed() -> None:
    from app.logging_config import scrub_text

    scrubbed = scrub_text("failed for Bearer abcdefghijklmnopqrst with 123456:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw")
    assert "abcdefghijklmnopqrst" not in scrubbed
    assert "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw" not in scrubbed
    assert "failed for" in scrubbed, "the surrounding diagnostic stays readable"

    long_hex = scrub_text("digest deadbeefdeadbeefdeadbeefdeadbeef leaked")
    assert "deadbeef" not in long_hex and "digest" in long_hex


def test_scrub_truncates_a_flood() -> None:
    from app.logging_config import scrub_text

    assert scrub_text("x" * 5000).endswith("chars]")


async def test_history_before_thread_id_raises_instead_of_inventing_a_conversation() -> None:
    """Item 12: resolving a thread from `history()` needed a user id, and guessing one (0)
    would have written the chat's conversation row attributed to Telegram user 0."""

    class _ExplodingRegistry:
        async def resolve(self, *, user_id: int, chat_id: int) -> str:  # pragma: no cover
            raise AssertionError(f"the registry must not be called: user_id={user_id}")

        async def rotate(self, *, chat_id: int) -> str:  # pragma: no cover
            raise AssertionError("not used")

    class _History:
        async def history(self, thread_id: str) -> list:  # pragma: no cover
            return []

    store = GraphConversationStore(_ExplodingRegistry(), _History(), session_factory=None)

    with pytest.raises(AssistantError, match="before thread_id"):
        await store.history(chat_id=4242)


def test_create_app_accepts_an_injected_saver_with_autorun() -> None:
    """Item 13: `create_app(checkpointer=X)` + MIGRATIONS_AUTORUN crashed with
    `AttributeError: 'State' object has no attribute 'checkpointer'`."""

    from app.main import create_app
    from tests.conftest import make_db_settings

    from langgraph.checkpoint.memory import InMemorySaver

    class _Saver(InMemorySaver):
        """A real saver (LangGraph validates the type) that counts `setup()` calls."""

        def __init__(self) -> None:
            super().__init__()
            self.setup_calls = 0

        async def setup(self) -> None:  # type: ignore[override]
            self.setup_calls += 1

    saver = _Saver()
    settings = make_db_settings(
        database_url="postgresql+psycopg://u:p@127.0.0.1:1/x",
        database_sync_url="postgresql+psycopg://u:p@127.0.0.1:1/x",
        migrations_autorun=True,
    )
    app = create_app(
        settings=settings,
        chat_model=OllamaStub(handler=_ok).client(),
        telegram_client=RecordingTelegram(),
        checkpointer=saver,
    )

    # Entering the context runs the lifespan, which is where the crash happened: with an
    # injected saver the old code called `setup_checkpointer(app.state.checkpointer)` and
    # `app.state.checkpointer` does not exist.
    with TestClient(app, raise_server_exceptions=True):
        pass

    assert saver.setup_calls == 1, "autorun must call setup() on the injected saver"
    assert not hasattr(app.state, "checkpointer"), "an injected saver is not ours to store"
