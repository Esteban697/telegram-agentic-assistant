"""Structured logging and redaction (PLAN.md §8.2, §7.4) plus the no-dead-configuration rule."""

from __future__ import annotations

import ast
import json
import logging
from pathlib import Path

from app.config import Settings
from app.logging_config import JsonFormatter, clear_request_context, redact, set_request_context
from app.llm.protocols import ChatMessage

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "app"


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="something happened",
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_formatter_emits_one_json_object_per_line() -> None:
    formatter = JsonFormatter(app_env="test")
    line = formatter.format(_record(status="ok", duration_ms=12.5))

    assert "\n" not in line
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["msg"] == "something happened"
    assert payload["status"] == "ok"
    assert payload["app_env"] == "test"


def test_request_context_is_attached_and_cleared() -> None:
    formatter = JsonFormatter(app_env="test")
    set_request_context(request_id="req-1", update_id=77, conversation_id="4242")
    try:
        payload = json.loads(formatter.format(_record()))
        assert payload["request_id"] == "req-1"
        assert payload["update_id"] == 77
        assert payload["conversation_id"] == "4242"
    finally:
        clear_request_context()

    assert "request_id" not in json.loads(formatter.format(_record()))


def test_blocked_keys_are_redacted() -> None:
    formatter = JsonFormatter(app_env="test")
    payload = json.loads(
        formatter.format(
            _record(
                token="super-secret-value",
                api_key="another-secret",
                webhook_secret="webhook",
                authorization="Bearer xyz",
                embedding=[0.1, 0.2],
                content="texto privado del usuario",
            )
        )
    )
    for key in ("token", "api_key", "webhook_secret", "authorization", "embedding", "content"):
        assert payload[key] == "[redacted]"

    rendered = json.dumps(payload)
    assert "super-secret-value" not in rendered
    assert "texto privado del usuario" not in rendered


def test_content_debug_is_the_only_way_content_becomes_visible() -> None:
    formatter = JsonFormatter(app_env="development", content_debug=True)
    payload = json.loads(formatter.format(_record(content="hola")))
    assert payload["content"] == "hola"

    # Even with the opt-in, a real secret stays redacted.
    payload = json.loads(formatter.format(_record(token="still-secret")))
    assert payload["token"] == "[redacted]"


def test_redact_helper_truncates_when_debug_is_on() -> None:
    long_text = "x" * 500
    assert redact(long_text, key="content", content_debug=True).endswith("chars]")
    assert redact(long_text, key="content", content_debug=False) == "[redacted]"


def test_exception_information_is_summarised_not_dumped() -> None:
    formatter = JsonFormatter(app_env="test")
    try:
        raise ValueError("boom with token abc")
    except ValueError:
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=__import__("sys").exc_info(),
        )
    payload = json.loads(formatter.format(record))
    assert payload["exc_type"] == "ValueError"
    assert "Traceback" not in json.dumps(payload)


def test_configure_logging_silences_httpx_request_lines() -> None:
    """The Bot API carries the token in the URL path, so httpx's INFO lines leak it.

    Found by reading the live log of a real run: the emitted line was
    ``HTTP Request: POST https://api.telegram.org/bot/sendMessage`` — with a configured token
    that line contains the credential (PLAN.md §7.4). ``configure_logging`` raises the level
    of the transport loggers, and this test is what stops a future edit from lowering it again.
    """

    from app.logging_config import configure_logging

    configure_logging(level="INFO", app_env="test")
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).level >= logging.WARNING, (
            f"{name} must not emit request URLs: the Telegram bot token is part of the path"
        )


def test_a_token_in_a_logged_url_would_be_caught_by_the_level_rule() -> None:
    """Belt and braces: with the level rule in place, an INFO request line is not emitted."""

    from app.logging_config import configure_logging

    configure_logging(level="INFO", app_env="test")
    httpx_logger = logging.getLogger("httpx")
    assert not httpx_logger.isEnabledFor(logging.INFO)
    assert httpx_logger.isEnabledFor(logging.WARNING)


# --------------------------------------------------------------------------- #
# No dead configuration (PLAN.md §8.1 defaults + §9.1)
# --------------------------------------------------------------------------- #
def _settings_fields() -> set[str]:
    return {
        name
        for name, value in Settings.model_fields.items()
        if not name.startswith("_")
    }


def test_env_example_documents_every_setting() -> None:
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    declared = {
        line.split("=", 1)[0].strip().upper()
        for line in text.splitlines()
        if "=" in line and not line.strip().startswith("#")
    }
    missing = sorted(field.upper() for field in _settings_fields() if field.upper() not in declared)
    assert missing == [], f"settings missing from .env.example: {missing}"


def test_env_example_contains_no_real_looking_secret() -> None:
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip().startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if key.strip().upper() in {"TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "OLLAMA_API_KEY"}:
            assert value == "", f"{key} must have an empty value in .env.example"


def test_every_setting_has_a_reader_outside_configuration() -> None:
    """The "no dead configuration" rule: a declared variable must be consumed somewhere.

    Secrets are read through the typed accessors on ``Settings`` (``bot_token``, ``api_key``,
    ``webhook_secret``) rather than by unwrapping them at each call site, so for those three
    fields the check is that the accessor exists *and* is used outside ``config.py``.
    """

    secret_fields = {
        "telegram_bot_token": "bot_token",
        "telegram_webhook_secret": "webhook_secret",
        "ollama_api_key": "api_key",
    }

    references: dict[str, list[str]] = {}
    for path in APP_DIR.rglob("*.py"):
        if path.name == "config.py" or "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                references.setdefault(node.attr, []).append(path.name)

    unread: list[str] = []
    for field in _settings_fields():
        accessor = secret_fields.get(field)
        if accessor is not None:
            if accessor not in references:
                unread.append(f"{field} (accessor {accessor} unread)")
            continue
        if field not in references:
            unread.append(field)

    assert unread == [], (
        "settings declared but never read (dead configuration): "
        f"{unread}. Add the consumer in the phase that uses it, or drop the field."
    )


def test_secret_accessors_exist_for_every_secret_field() -> None:
    settings = Settings(telegram_bot_token="t", telegram_webhook_secret="s", ollama_api_key="k")
    assert settings.bot_token == "t"
    assert settings.webhook_secret == "s"
    assert settings.api_key == "k"


def test_settings_model_imports_no_application_module() -> None:
    """Layer 0: ``config.py`` may not import other ``app`` packages."""

    tree = ast.parse((APP_DIR / "config.py").read_text(encoding="utf-8"))
    allowed = {"app.errors", "app.policy.authz", "app.policy.limits"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app"):
            if node.module not in allowed:
                offenders.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app") and alias.name not in allowed:
                    offenders.append(alias.name)
    assert offenders == [], f"config.py imports outside layer 0: {offenders}"


def test_chat_message_is_immutable() -> None:
    """The ports are frozen dataclasses so a graph node cannot mutate shared state."""

    message = ChatMessage(role="user", content="hola")
    assert message.as_dict() == {"role": "user", "content": "hola"}
    try:
        message.content = "cambiado"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("ChatMessage should be frozen")
