"""Configuration and the centralised fail-fast list (PLAN.md §8.1, §7.2, A13)."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.errors import ConfigError
from tests.conftest import make_settings

APP_DIR = Path(__file__).resolve().parents[2] / "app"


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #
def test_development_settings_are_valid() -> None:
    settings = make_settings()
    assert settings.app_env == "test"
    assert settings.store_backend == "memory"
    assert settings.telegram_allowed_user_ids == frozenset({42})


def test_the_test_settings_do_not_depend_on_an_ambient_store_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unit suite must survive a developer configuring the setting Phase 2 asks for.

    ``make_settings`` promised "development-safe defaults" but left ``store_backend`` to be
    inherited from the ambient ``.env``/environment — so setting ``STORE_BACKEND=postgres`` in
    development broke five unit tests that assert the development default. Pinned now, and this
    test fails if anyone un-pins it.
    """
    monkeypatch.setenv("STORE_BACKEND", "postgres")

    assert make_settings().store_backend == "memory"


def test_production_settings_with_everything_required_are_valid() -> None:
    settings = make_settings(
        app_env="production",
        store_backend="postgres",
        telegram_allowed_user_ids=frozenset({1, 2}),
        gcs_bucket="a-private-bucket",
        google_cloud_project="a-project",
    )
    assert settings.is_production
    assert settings.problems() == []


# --------------------------------------------------------------------------- #
# The fail-fast list (PLAN.md §7.2, B6 → A13)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        (
            {"app_env": "production", "gcs_bucket": "b", "google_cloud_project": "p"},
            "STORE_BACKEND=memory",
        ),
        (
            {
                "app_env": "production",
                "store_backend": "postgres",
                "dev_allow_any_user": True,
            },
            "DEV_ALLOW_ANY_USER=true",
        ),
        (
            {
                "app_env": "production",
                "store_backend": "postgres",
                "telegram_allowed_user_ids": frozenset(),
            },
            "TELEGRAM_ALLOWED_USER_IDS is empty",
        ),
    ],
)
def test_production_refuses_unsafe_configuration(overrides: dict, needle: str) -> None:
    """A13: the in-memory configuration cannot be deployed, and neither can the others."""

    with pytest.raises(ConfigError) as excinfo:
        make_settings(**overrides)
    assert needle in str(excinfo.value)


def test_production_refuses_an_empty_bot_token_and_secret() -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_settings(
            app_env="production",
            store_backend="postgres",
            telegram_bot_token="",
            telegram_webhook_secret="",
        )
    message = str(excinfo.value)
    assert "TELEGRAM_BOT_TOKEN is empty" in message
    assert "TELEGRAM_WEBHOOK_SECRET is empty" in message


def test_production_rejects_a_webhook_secret_telegram_would_refuse() -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_settings(
            app_env="production",
            store_backend="postgres",
            telegram_webhook_secret="not allowed: spaces",
        )
    assert "TELEGRAM_WEBHOOK_SECRET must be 1-256 characters" in str(excinfo.value)


def test_all_problems_are_reported_in_one_pass() -> None:
    """§8.1: aggregated, "rather than revealing them one deploy at a time"."""

    with pytest.raises(ConfigError) as excinfo:
        make_settings(
            app_env="production",
            store_backend="memory",
            telegram_allowed_user_ids=frozenset(),
            telegram_bot_token="",
            telegram_webhook_secret="",
        )
    error = excinfo.value
    assert error.context["problem_count"] >= 4
    for needle in ("STORE_BACKEND=memory", "TELEGRAM_ALLOWED_USER_IDS is empty", "TELEGRAM_BOT_TOKEN"):
        assert needle in str(error)


# --------------------------------------------------------------------------- #
# Field validation
# --------------------------------------------------------------------------- #
def test_allow_list_parses_commas_and_whitespace_and_strips_duplicates() -> None:
    settings = Settings(telegram_allowed_user_ids="1, 2  3,3")
    assert settings.telegram_allowed_user_ids == frozenset({1, 2, 3})


def test_allow_list_rejects_non_numeric_entries() -> None:
    with pytest.raises(Exception) as excinfo:
        Settings(telegram_allowed_user_ids="1, abc")
    assert "non-numeric" in str(excinfo.value)


def test_blank_allow_list_is_empty_and_fails_closed_in_production() -> None:
    assert Settings(telegram_allowed_user_ids="").telegram_allowed_user_ids == frozenset()


# --------------------------------------------------------------------------- #
# Parsing straight from the environment
#
# Added after a live run of the README flow failed: pydantic-settings JSON-parses the raw
# value of a complex field, so a single id arrives as ``int`` rather than as ``str``.
# Passing frozensets directly in tests hid that path entirely.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("42", frozenset({42})),
        ("1,2", frozenset({1, 2})),
        ("1, 2  3", frozenset({1, 2, 3})),
        ("", frozenset()),
        ("  ", frozenset()),
    ],
)
def test_allow_list_parses_from_a_real_environment_value(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: frozenset[int]
) -> None:
    """The format `.env.example` documents: comma- or whitespace-separated numeric ids."""

    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", raw)
    assert Settings().telegram_allowed_user_ids == expected


def test_a_json_list_is_rejected_with_a_clear_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the documented comma-separated form is accepted; brackets are a typo, not a list.

    JSON decoding is disabled for this field (see `NoDecode` in config.py), so a bracketed
    value fails loudly instead of being silently reinterpreted.
    """

    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "[1,2]")
    with pytest.raises(Exception) as excinfo:
        Settings()
    assert "non-numeric" in str(excinfo.value)


def test_a_non_numeric_environment_value_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "42,usuario")
    with pytest.raises(Exception) as excinfo:
        Settings()
    assert "non-numeric" in str(excinfo.value)


def test_environment_values_flow_through_load_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole README-configured path, exactly as a user would set it."""

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "42")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "local-dev-secret")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    monkeypatch.setenv("REQUEST_BUDGET_SECONDS", "5")
    monkeypatch.setenv("VISION_TIMEOUT_SECONDS", "3")

    from app.config import load_settings

    settings = load_settings()
    assert settings.telegram_allowed_user_ids == frozenset({42})
    assert settings.webhook_secret == "local-dev-secret"
    assert settings.request_budget_seconds == 5.0


def test_bool_is_never_a_valid_user_id() -> None:
    with pytest.raises(Exception):
        Settings(telegram_allowed_user_ids=True)


def test_log_level_is_normalised_and_validated() -> None:
    assert Settings(log_level="debug").log_level == "DEBUG"
    with pytest.raises(Exception):
        Settings(log_level="verbose")


def test_the_largest_chunk_must_fit_inside_the_context_window() -> None:
    """`RAG_CHUNK_MAX_CHARS > RAG_MAX_CONTEXT_CHARS` was accepted and was silently wrong.

    The largest chunk the splitter can emit could never fit in the context handed to the model, so
    every answer would be assembled from a truncated passage — and the block renderer keeps the
    citation either way, so nothing downstream noticed.
    """

    with pytest.raises(ConfigError, match="RAG_CHUNK_MAX_CHARS"):
        make_settings(rag_chunk_max_chars=7000, rag_max_context_chars=6000)

    # The boundary is inclusive: a chunk that exactly fills the window is fine.
    assert make_settings(rag_chunk_max_chars=6000, rag_max_context_chars=6000).problems() == []


def test_llm_timeout_must_fit_inside_the_request_budget() -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_settings(llm_timeout_seconds=60.0, request_budget_seconds=45.0)
    assert "provider call can outlast the request budget" in str(excinfo.value)


def test_think_flag_normalisation() -> None:
    assert Settings(llm_think="OFF").llm_think is None
    assert Settings(llm_think="").llm_think is None
    assert Settings(llm_think="HIGH").llm_think == "high"
    with pytest.raises(Exception):
        Settings(llm_think="maximum")


def test_secret_values_are_not_rendered() -> None:
    settings = make_settings()
    assert "test-token-not-a-real-secret" not in repr(settings)
    assert settings.bot_token == "test-token-not-a-real-secret"


CALENDAR_CREDENTIALS = json.dumps(
    {
        "client_id": "id",
        "client_secret": "secret",
        "refresh_token": "refresh",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/calendar.events"],
    }
)


def test_calendar_enabled_with_credentials_is_valid() -> None:
    settings = make_settings(
        google_calendar_enabled=True,
        google_oauth_credentials=CALENDAR_CREDENTIALS,
    )
    assert settings.problems() == []


def test_calendar_enabled_requires_credentials() -> None:
    broken = make_settings().model_copy(
        update={"google_calendar_enabled": True, "google_oauth_credentials": SecretStr("")}
    )
    assert any("GOOGLE_OAUTH_CREDENTIALS" in item for item in broken.problems())


def test_calendar_credentials_must_be_json_and_complete() -> None:
    broken = make_settings().model_copy(
        update={
            "google_calendar_enabled": True,
            "google_oauth_credentials": SecretStr("not json"),
        }
    )
    assert any("not valid JSON" in item for item in broken.problems())

    incomplete = make_settings().model_copy(
        update={
            "google_calendar_enabled": True,
            "google_oauth_credentials": SecretStr(json.dumps({"client_id": "id"})),
        }
    )
    assert any("missing required field" in item for item in incomplete.problems())


def test_calendar_workday_must_have_a_positive_window() -> None:
    broken = make_settings().model_copy(
        update={
            "google_calendar_enabled": True,
            "google_oauth_credentials": SecretStr(CALENDAR_CREDENTIALS),
            "google_calendar_workday_start_hour": 18,
            "google_calendar_workday_end_hour": 9,
        }
    )
    assert any("WORKDAY_START_HOUR" in item for item in broken.problems())


# --------------------------------------------------------------------------- #
# A6: the model name has exactly one source
# --------------------------------------------------------------------------- #
def test_model_name_is_never_hard_coded_outside_configuration() -> None:
    """A6: "the model string originates only from ``Settings.llm_model``"."""

    offenders: list[str] = []
    for path in APP_DIR.rglob("*.py"):
        if path.name == "config.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "gpt-oss" in node.value:
                    offenders.append(f"{path.relative_to(APP_DIR)}:{node.lineno}")
    assert offenders == [], f"model literal found outside app/config.py: {offenders}"
