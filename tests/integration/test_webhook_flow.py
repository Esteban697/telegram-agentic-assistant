"""End-to-end webhook flow with mocked transports (PLAN.md §10 Phase 1, A2-A7, A11, A12).

No external credentials: the Ollama transport is an ``httpx.MockTransport`` wrapped in the
**real** client (so the outbound request body is genuine) and the Telegram side is a recording
double (so the assertions can count calls). The app is built by the real factory, so the
lifespan, the store selection and the wiring are all exercised.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.bot import messages
from app.main import create_app
from tests.conftest import (
    CHAT_ID,
    TEST_DATABASE_URL,
    USER_ID,
    WEBHOOK_SECRET,
    OllamaStub,
    RecordingTelegram,
    chat_response,
    make_db_settings,
    make_settings,
    truncate_all,
)

# (f) The same suite, the other backend: `TEST_STORE_BACKEND=postgres` reruns every test here
# against PostgreSQL instead of the in-memory stores, with no test bodies changed.
STORE_BACKEND = os.environ.get("TEST_STORE_BACKEND", "memory")

pytestmark = pytest.mark.integration

URL = "/telegram/webhook"
HEADERS = {"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _message_update(*, update_id: int, text: str, user_id: int = USER_ID, chat_id: int = CHAT_ID) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "from": {"id": user_id, "is_bot": False, "first_name": "Usuario"},
            "chat": {"id": chat_id, "type": "private"},
            "date": 1_700_000_000,
            "text": text,
        },
    }


class Harness:
    """A running app plus the two doubles, so a test can assert on both sides."""

    def __init__(self, handler=None, **setting_overrides: Any) -> None:
        self._handler = handler
        self.telegram = RecordingTelegram()
        self.stub: OllamaStub | None = None
        if STORE_BACKEND == "postgres":
            if not TEST_DATABASE_URL:
                pytest.skip("TEST_DATABASE_URL is not set: the postgres run needs a database")
            # MIGRATIONS_AUTORUN only exists so the checkpointer's idempotent setup() runs at
            # startup here; production validation refuses it.
            setting_overrides.setdefault("migrations_autorun", True)
            self.settings = make_db_settings(**setting_overrides)
        else:
            self.settings = make_settings(**setting_overrides)
        self._client: TestClient | None = None

    def __enter__(self) -> "Harness":
        if STORE_BACKEND == "postgres":
            truncate_all(self.settings)
        handler = self._handler
        if handler is None:
            handler = lambda request: httpx.Response(200, json=chat_response())  # noqa: E731
        self.stub = OllamaStub(handler=handler)
        chat_model = self.stub.client(model=self.settings.llm_model)
        self.app = create_app(
            settings=self.settings, chat_model=chat_model, telegram_client=self.telegram
        )
        self._client = TestClient(self.app, raise_server_exceptions=False)
        self._client.__enter__()
        return self

    def __exit__(self, *_exc: object) -> None:
        assert self._client is not None
        self._client.__exit__(None, None, None)

    def post(self, update: dict, *, headers: dict | None = None) -> httpx.Response:
        return self._client.post(URL, json=update, headers=HEADERS if headers is None else headers)

    def post_raw(self, body: bytes, *, headers: dict | None = None) -> httpx.Response:
        return self._client.post(URL, content=body, headers=HEADERS if headers is None else headers)

    @property
    def chat_call_count(self) -> int:
        assert self.stub is not None
        return len(self.stub.chat_bodies)

    def status_of(self, update_id: int) -> str | None:
        return asyncio.run(self.app.state.dedupe.status_of(update_id))


# --------------------------------------------------------------------------- #
# A2 — an unauthorised user gains no access
# --------------------------------------------------------------------------- #
def test_unauthorised_user_gets_no_access() -> None:
    with Harness() as harness:
        response = harness.post(_message_update(update_id=1, text="hola", user_id=999))

    assert response.status_code == 200, "Telegram must be answered so it stops retrying"
    assert harness.chat_call_count == 0, "no model call for an unauthorised user"
    assert harness.telegram.count_of("sendMessage") == 0, "no reply at all"
    assert harness.status_of(1) == "ignored"


def test_unauthorised_user_is_denied_even_with_an_empty_allow_list() -> None:
    with Harness(telegram_allowed_user_ids=frozenset()) as harness:
        response = harness.post(_message_update(update_id=2, text="hola"))

    assert response.status_code == 200
    assert harness.chat_call_count == 0
    assert harness.telegram.count_of("sendMessage") == 0


# --------------------------------------------------------------------------- #
# A3 — an authorised user can converse
# --------------------------------------------------------------------------- #
def test_authorised_user_converses_and_context_carries_over() -> None:
    replies = iter(["respuesta uno", "respuesta dos"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=chat_response(content=next(replies)))

    with Harness(handler=handler) as harness:
        first = harness.post(_message_update(update_id=1, text="primer mensaje"))
        second = harness.post(_message_update(update_id=2, text="segundo mensaje"))

    assert first.status_code == 200 and second.status_code == 200
    assert harness.chat_call_count == 2
    assert harness.telegram.count_of("sendMessage") == 2
    assert harness.telegram.sent_texts == ["respuesta uno", "respuesta dos"]

    assert harness.stub is not None
    first_body, second_body = harness.stub.chat_bodies
    assert first_body["messages"][0]["role"] == "system"

    contents = [entry["content"] for entry in second_body["messages"]]
    assert "primer mensaje" in contents, "the first user turn must be in the second prompt"
    assert "respuesta uno" in contents, "the first assistant turn must be in the second prompt"


def test_reset_drops_the_retained_context() -> None:
    with Harness() as harness:
        harness.post(_message_update(update_id=1, text="mensaje anterior"))
        reset = harness.post(_message_update(update_id=2, text="/reset"))
        harness.post(_message_update(update_id=3, text="mensaje nuevo"))

    assert reset.status_code == 200
    assert messages.render("reset.done") in harness.telegram.sent_texts

    assert harness.stub is not None
    # /reset is a command, so it produces no model call: the two model turns are message 1
    # and message 3, and it is the second of those that must be free of the first's history.
    assert harness.chat_call_count == 2
    third_body = harness.stub.chat_bodies[1]
    contents = [entry["content"] for entry in third_body["messages"]]
    assert "mensaje anterior" not in contents, "/reset must clear the context list"
    assert "mensaje nuevo" in contents


# --------------------------------------------------------------------------- #
# A4 — the webhook secret
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("headers", [{}, {"X-Telegram-Bot-Api-Secret-Token": "wrong"}])
def test_wrong_or_missing_secret_yields_403_and_no_processing(headers: dict) -> None:
    with Harness() as harness:
        response = harness.post(_message_update(update_id=1, text="hola"), headers=headers)

    assert response.status_code == 403
    assert response.content == b"", "the body must stay empty"
    assert harness.chat_call_count == 0
    assert harness.telegram.calls == []
    assert harness.status_of(1) is None, "nothing must be recorded for an unsigned request"


def test_non_ascii_secret_header_is_a_403_not_a_500() -> None:
    """N11 at the HTTP boundary: a non-ASCII byte must be denied, not crash.

    The header is sent as raw **bytes** because HTTP header values are bytes on the wire and
    httpx refuses to encode a non-ASCII ``str`` at all. The unit test for ``_secret_matches``
    covers the emoji and lone-surrogate cases, which is where the ``TypeError`` risk lives.
    """

    with Harness() as harness:
        response = harness.post_raw(
            json.dumps(_message_update(update_id=1, text="hola")).encode(),
            headers={
                b"X-Telegram-Bot-Api-Secret-Token": b"secreto-\xf1",
                b"Content-Type": b"application/json",
            },
        )

    assert response.status_code == 403
    assert harness.chat_call_count == 0


def test_oversized_body_is_rejected_before_parsing() -> None:
    with Harness(max_webhook_body_bytes=128) as harness:
        response = harness.post_raw(b"{" + b"x" * 500 + b"}")

    assert response.status_code == 413
    assert harness.chat_call_count == 0


def test_malformed_json_is_answered_200_and_dropped() -> None:
    """Retrying a body that can never parse would be a retry storm, not a recovery."""

    with Harness() as harness:
        response = harness.post_raw(b"{not json")

    assert response.status_code == 200
    assert harness.telegram.calls == []


# --------------------------------------------------------------------------- #
# A5 — idempotency
# --------------------------------------------------------------------------- #
def test_a_duplicated_update_is_processed_once() -> None:
    update = _message_update(update_id=1, text="hola")

    with Harness() as harness:
        first = harness.post(update)
        second = harness.post(update)

    assert first.status_code == 200 and second.status_code == 200
    assert harness.chat_call_count == 1, "the model must be called exactly once"
    assert harness.telegram.count_of("sendMessage") == 1
    assert harness.status_of(1) == "processed"


# --------------------------------------------------------------------------- #
# A6 — the model is configuration only
# --------------------------------------------------------------------------- #
def test_the_configured_model_is_the_one_sent() -> None:
    with Harness(llm_model="custom-model:test") as harness:
        harness.post(_message_update(update_id=1, text="hola"))

    assert harness.stub is not None
    assert harness.stub.chat_bodies_for_model == ["custom-model:test"]


# --------------------------------------------------------------------------- #
# A7 — provider failures are controlled
# --------------------------------------------------------------------------- #
def test_provider_timeout_produces_the_timeout_message_and_a_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with Harness(handler=handler) as harness:
        response = harness.post(_message_update(update_id=1, text="hola"))

    assert response.status_code == 200
    assert harness.telegram.sent_texts == [messages.render("error.timeout")]
    assert "Traceback" not in " ".join(harness.telegram.sent_texts)
    assert harness.status_of(1) == "failed"


def test_provider_rate_limit_produces_a_specific_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "slow down"})

    with Harness(handler=handler) as harness:
        response = harness.post(_message_update(update_id=1, text="hola"))

    assert response.status_code == 200
    assert harness.telegram.sent_texts == [messages.render("error.model_unavailable")]


def test_provider_server_error_produces_a_specific_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    with Harness(handler=handler) as harness:
        response = harness.post(_message_update(update_id=1, text="hola"))

    assert response.status_code == 200
    assert harness.telegram.sent_texts == [messages.render("error.model_unavailable")]


def test_a_provider_failure_is_logged_with_an_error_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A7's third clause: the failure "is logged with an ``error_code``".

    Nothing asserted it before the Phase 1 review — the criterion was demonstrated for the
    user-visible message and for the HTTP status only, so the observability half of the
    acceptance criterion was unverified.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "slow down"})

    with caplog.at_level(logging.WARNING):
        with Harness(handler=handler) as harness:
            harness.post(_message_update(update_id=1, text="hola"))
            status = harness.status_of(1)

    codes = [getattr(record, "error_code", None) for record in caplog.records]
    assert caplog.records, "the failure must produce log records"
    assert "ollama_rate_limited" in codes, f"expected a typed error_code, got {codes}"
    assert status == "failed", "the ledger must record the failure"

    # And the log line must not carry the provider's body (review item 1).
    emitted = "\n".join(str(record.__dict__) for record in caplog.records)
    assert "slow down" not in emitted, "a provider error body must not be logged"


def test_unexpected_handler_failure_still_answers_telegram_and_the_user() -> None:
    class ExplodingTelegram(RecordingTelegram):
        async def send_message(self, **kwargs: Any):  # noqa: ANN202
            raise RuntimeError("boom inside telegram")

    telegram = ExplodingTelegram()
    settings = make_settings()
    stub = OllamaStub(handler=lambda request: httpx.Response(200, json=chat_response()))
    app = create_app(
        settings=settings, chat_model=stub.client(), telegram_client=telegram
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(URL, json=_message_update(update_id=1, text="hola"), headers=HEADERS)

    assert response.status_code == 200, "a crash must never turn into a Telegram retry storm"
    assert asyncio.run(app.state.dedupe.status_of(1)) == "failed"


# --------------------------------------------------------------------------- #
# A11 — commands in Spanish
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("command", "key"),
    [
        ("/start", "start"),
        ("/help", "help"),
        ("/reset", "reset.done"),
    ],
)
def test_commands_reply_from_the_catalogue(command: str, key: str) -> None:
    with Harness() as harness:
        response = harness.post(_message_update(update_id=1, text=command))

    assert response.status_code == 200
    assert harness.telegram.sent_texts == [messages.render(key)]
    assert harness.chat_call_count == 0, "a command is not a model turn"


@pytest.mark.parametrize("command", ["/tasks", "/memory"])
def test_storage_commands_answer_either_from_storage_or_with_an_explicit_refusal(
    command: str,
) -> None:
    """Phase 2 makes these real.

    With a database they answer from it; without one (`STORE_BACKEND=memory`, development) they
    say so explicitly instead of pretending. The same test body covers both backends — which is
    the point of criterion (f).
    """

    with Harness() as harness:
        harness.post(_message_update(update_id=1, text=command))

    if STORE_BACKEND == "postgres":
        assert harness.telegram.sent_texts == [messages.render("tasks.empty" if command == "/tasks" else "memory.empty")]
    else:
        assert harness.telegram.sent_texts == [messages.render("error.storage_unavailable")]


def test_commands_accept_the_botname_suffix() -> None:
    with Harness() as harness:
        harness.post(_message_update(update_id=1, text="/help@mi_bot"))

    assert harness.telegram.sent_texts == [messages.render("help")]


def test_forget_without_an_argument_explains_the_syntax() -> None:
    with Harness() as harness:
        harness.post(_message_update(update_id=1, text="/forget"))

    expected = (
        messages.render("forget.needs_key")
        if STORE_BACKEND == "postgres"
        else messages.render("error.storage_unavailable")
    )
    assert harness.telegram.sent_texts == [expected]


def test_an_unknown_command_answers_with_help() -> None:
    with Harness() as harness:
        harness.post(_message_update(update_id=1, text="/nope"))

    assert harness.telegram.sent_texts == [messages.render("help")]


def test_a_callback_query_is_acknowledged_then_dispatched() -> None:
    update = {
        "update_id": 5,
        "callback_query": {
            "id": "cb-1",
            "from": {"id": USER_ID},
            "message": {"message_id": 50, "chat": {"id": CHAT_ID}},
            "data": "cmd:help",
        },
    }

    with Harness() as harness:
        response = harness.post(update)

    assert response.status_code == 200
    assert harness.telegram.count_of("answerCallbackQuery") == 1, (
        "the callback must be acknowledged before anything else"
    )
    assert harness.telegram.sent_texts == [messages.render("help")]


def test_an_unknown_callback_data_is_refused_not_ignored() -> None:
    update = {
        "update_id": 6,
        "callback_query": {
            "id": "cb-2",
            "from": {"id": USER_ID},
            "message": {"message_id": 51, "chat": {"id": CHAT_ID}},
            "data": "ap:forged",
        },
    }

    with Harness() as harness:
        harness.post(update)

    assert harness.telegram.sent_texts == [messages.render("callback.not_available")]


def test_an_edited_message_is_noticed_not_replayed() -> None:
    update = {
        "update_id": 7,
        "edited_message": {
            "message_id": 60,
            "from": {"id": USER_ID},
            "chat": {"id": CHAT_ID},
            "text": "texto corregido",
        },
    }

    with Harness() as harness:
        response = harness.post(update)

    assert response.status_code == 200
    assert harness.chat_call_count == 0, "an edit must not re-run the turn"
    assert harness.telegram.sent_texts == [messages.render("edited.notice")]
    assert harness.status_of(7) == "noticed"


def test_a_non_text_message_is_answered_explicitly() -> None:
    update = {
        "update_id": 8,
        "message": {
            "message_id": 70,
            "from": {"id": USER_ID},
            "chat": {"id": CHAT_ID},
            "photo": [{"file_id": "abc", "width": 1, "height": 1}],
        },
    }

    with Harness() as harness:
        harness.post(update)

    assert harness.telegram.sent_texts == [messages.render("unsupported.content")]
    assert harness.chat_call_count == 0


def test_an_over_long_message_is_refused_with_a_specific_reply() -> None:
    with Harness(max_message_chars=50) as harness:
        harness.post(_message_update(update_id=9, text="x" * 51))

    assert harness.telegram.sent_texts == [messages.render("error.message_too_long")]
    assert harness.chat_call_count == 0


def test_an_update_without_an_update_id_is_dropped() -> None:
    with Harness() as harness:
        response = harness.post({"message": {"from": {"id": USER_ID}, "chat": {"id": CHAT_ID}}})

    assert response.status_code == 200
    assert harness.chat_call_count == 0


def test_an_unsupported_update_type_is_ignored() -> None:
    with Harness() as harness:
        response = harness.post({"update_id": 11, "poll": {"id": "p1"}})

    assert response.status_code == 200
    assert harness.telegram.calls == []


# --------------------------------------------------------------------------- #
# A12 — rate limiting
# --------------------------------------------------------------------------- #
def test_rate_limit_answers_200_makes_no_model_call_and_notifies_once() -> None:
    with Harness(rate_limit_per_minute=1) as harness:
        allowed = harness.post(_message_update(update_id=1, text="primero"))
        denied = harness.post(_message_update(update_id=2, text="segundo"))
        denied_again = harness.post(_message_update(update_id=3, text="tercero"))

    assert allowed.status_code == denied.status_code == denied_again.status_code == 200
    assert harness.chat_call_count == 1, "only the allowed request reaches the model"
    notices = [text for text in harness.telegram.sent_texts if text == messages.render("error.rate_limited")]
    assert len(notices) == 1, "one notice per window, not one per request"
    assert harness.status_of(2) == "ignored"
    assert harness.status_of(3) == "ignored"


# --------------------------------------------------------------------------- #
# Health endpoints
# --------------------------------------------------------------------------- #
def test_health_and_readiness() -> None:
    with Harness() as harness:
        health = harness._client.get("/healthz")
        ready = harness._client.get("/readyz")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}


def test_health_endpoints_do_not_leak_internals() -> None:
    with Harness() as harness:
        body = json.dumps(harness._client.get("/readyz").json())

    for forbidden in (WEBHOOK_SECRET, "test-token-not-a-real-secret", "ollama", "postgres"):
        assert forbidden not in body
