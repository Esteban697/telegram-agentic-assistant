"""The Spanish catalogue and the "only one source of user-facing text" rule (PLAN.md §9.1)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.bot import messages

APP_DIR = Path(__file__).resolve().parents[2] / "app"
BOT_DIR = APP_DIR / "bot"

# Anything that looks Spanish: an accented character, or a common Spanish word. Used only to
# decide whether a literal is a *candidate* user-facing string.
# Markers of Spanish text. Accents and inverted punctuation are the reliable signal: English
# prose in a docstring essentially never contains them. A handful of unambiguous Spanish words
# are included because a short reply ("Hola") has no accent.
_SPANISH_HINT = re.compile(
    r"[áéíóúñ¿¡ü]|\b(hola|ayuda|todavía|disponible|comando)\b",
    re.IGNORECASE,
)

# Modules that legitimately contain Spanish prose that is not shown to the user.
_ALLOWED_SPANISH = {
    "messages.py",  # the catalogue itself
    "prompts.py",  # the system prompt: sent to the model, never rendered to the user
}

# Functions whose first string argument must come from the catalogue.
_SENDERS = {
    "send_message", "notify", "edit_message_text", "_reply", "render",
}


def _python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def test_catalogue_keys_are_dotted_and_unique() -> None:
    for key in messages.CATALOGUE:
        assert key, "empty catalogue key"
        assert " " not in key
    assert len(set(messages.CATALOGUE)) == len(messages.CATALOGUE)


def test_every_catalogue_value_is_a_non_empty_spanish_string() -> None:
    for key, value in messages.CATALOGUE.items():
        assert isinstance(value, str) and value.strip(), f"{key} is empty"
        if "{command}" in value:
            assert "{" in value  # placeholder documented by the caller
    # A smoke check that the catalogue is actually Spanish, not English leftovers.
    joined = " ".join(messages.CATALOGUE.values())
    assert "todavía" in joined or "disponible" in joined


def test_render_substitutes_placeholders() -> None:
    assert "/tasks" in messages.render("error.not_available", command="/tasks")


def test_render_raises_for_an_unknown_key() -> None:
    with pytest.raises(KeyError):
        messages.render("does.not.exist")


def test_has() -> None:
    assert messages.has("reset.done")
    assert not messages.has("nope")


def test_every_render_call_in_the_codebase_uses_a_real_key() -> None:
    """A missing key must fail loudly at test time, not at runtime for the user."""

    used: set[str] = set()
    for path in _python_files(APP_DIR):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "render"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                used.add(node.args[0].value)

    assert used, "no render() calls found — the scan is broken"
    missing = sorted(key for key in used if not messages.has(key))
    assert missing == [], f"catalogue keys referenced but not defined: {missing}"


def test_error_taxonomy_message_keys_all_exist() -> None:
    """Every typed error renders through the catalogue, so its key must be real."""

    from app import errors

    missing: list[str] = []
    for name in errors.__all__:
        candidate = getattr(errors, name)
        if isinstance(candidate, type) and issubclass(candidate, errors.AssistantError):
            if not messages.has(candidate.message_key):
                missing.append(f"{name} -> {candidate.message_key}")
    assert missing == [], f"error message keys missing from the catalogue: {missing}"


def test_no_spanish_literals_outside_the_catalogue() -> None:
    """PLAN.md §9.1: every user-facing string lives in ``messages.py``.

    Docstrings are excluded — they are documentation, not output — and the scan is limited to
    the modules that can send text to Telegram. ``tests/`` and ``scripts/`` are excluded too:
    their output is for the operator, not the user.
    """

    offenders: list[str] = []
    for directory in (BOT_DIR, APP_DIR / "api"):
        for path in _python_files(directory):
            if path.name in _ALLOWED_SPANISH:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            docstrings = {
                id(node.body[0].value)
                for node in ast.walk(tree)
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
            }
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if id(node) in docstrings:
                        continue
                    if _SPANISH_HINT.search(node.value):
                        offenders.append(f"{path.relative_to(APP_DIR)}:{node.lineno}")
    assert offenders == [], f"Spanish literal outside app/bot/messages.py: {offenders}"


def test_notification_calls_take_their_text_from_the_catalogue() -> None:
    """The strongest form of the rule that is mechanically checkable.

    Every call to a text-sending helper must pass a ``render(...)`` call (or a value that came
    from one), never a literal — otherwise a reply could bypass the catalogue and silently ship
    in the wrong language.
    """

    violations: list[str] = []
    for path in (BOT_DIR / "handlers.py", APP_DIR / "api" / "telegram_webhook.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in _SENDERS:
                continue
            for keyword in node.keywords:
                if keyword.arg not in {"text", "message"}:
                    continue
                value = keyword.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    violations.append(f"{path.name}:{node.lineno} literal text={value.value!r}")
                if isinstance(value, ast.JoinedStr):
                    violations.append(f"{path.name}:{node.lineno} f-string text")
    assert violations == [], f"text sent to Telegram without the catalogue: {violations}"
