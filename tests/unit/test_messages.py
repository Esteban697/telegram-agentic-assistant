"""The Spanish catalogue, and the static rules that keep it the only source of user text.

PLAN.md §9.1 asks for "an AST walk over ``app/``". The walks live here as small functions so
they can be tested against **synthetic sources** — a rule that only ever runs over a tree that
currently passes is not a tested rule, and the Phase 1 review found two holes exactly that way:

* the first version inspected only **keyword** arguments, so ``self._reply(chat_id, "Hola")``
  — the dominant calling convention in ``handlers.py`` — was invisible;
* the Spanish detector required an accent or one of five words, so ``"No entiendo eso"`` passed.

Both are fixed below, and both have a synthetic test that fails against the old behaviour.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.bot import messages

APP_DIR = Path(__file__).resolve().parents[2] / "app"

# Modules that legitimately contain Spanish prose that is not shown to the user.
_ALLOWED_SPANISH = {
    "messages.py",  # the catalogue itself
    "prompts.py",  # the system prompt: sent to the model, never rendered to the user
}

# Helpers that send text to Telegram. In every one of them a positional string argument is the
# message body, so both positions and keywords must be inspected.
_TEXT_SENDERS = frozenset({"send_message", "notify", "edit_message_text", "_reply"})
_TEXT_KEYWORDS = frozenset({"text", "message", "body"})

# Unambiguous Spanish words. Deliberately excludes words that also exist in English ("error",
# "context") because log messages are English and would produce false positives — the detector
# only has to be good enough to catch a literal that bypassed the catalogue.
_SPANISH_WORDS = (
    "hola",
    "gracias",
    "ahora",
    "siempre",
    "mensaje",
    "mensajes",
    "usuario",
    "usuarios",
    "comando",
    "comandos",
    "todavía",
    "disponible",
    "respuesta",
    "respuestas",
    "contexto",
    "recordatorio",
    "recordatorios",
    "entiendo",
    "quiero",
    "puedes",
    "dime",
    "necesito",
    "ayuda",
    "vale",
)
_SPANISH_HINT = re.compile(
    r"[áéíóúñ¿¡ü]|\b(" + "|".join(_SPANISH_WORDS) + r")\b",
    re.IGNORECASE,
)

# The only placeholders the catalogue is allowed to use, so a typo cannot ship a literal
# "{comamnd}" to a user.
_ALLOWED_PLACEHOLDERS = frozenset({"command"})


# --------------------------------------------------------------------------- #
# The scanners (pure functions over source text, so they can be tested directly)
# --------------------------------------------------------------------------- #
def _docstring_node_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _callee_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", "")


def find_literal_text_arguments(source: str) -> list[str]:
    """Return every text argument passed to a sending helper as a literal.

    Covers keyword arguments (``text="hola"``), positional arguments (``self._reply(cid,
    "hola")``) and f-strings in either position.
    """

    tree = ast.parse(source)
    findings: list[str] = []

    def _check(value: ast.AST, *, where: str, lineno: int) -> None:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            findings.append(f"line {lineno}: literal {where}={value.value!r}")
        elif isinstance(value, ast.JoinedStr):
            findings.append(f"line {lineno}: f-string {where}")

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _callee_name(node) not in _TEXT_SENDERS:
            continue
        for keyword in node.keywords:
            if keyword.arg in _TEXT_KEYWORDS:
                _check(keyword.value, where=keyword.arg, lineno=node.lineno)
        for index, argument in enumerate(node.args):
            # Every positional argument of these helpers is either an id or the text; only
            # strings and f-strings are suspicious, so ids pass through untouched.
            _check(argument, where=f"arg{index}", lineno=node.lineno)
    return findings


def find_spanish_literals(source: str) -> list[str]:
    """Return every Spanish-looking string literal outside a docstring."""

    tree = ast.parse(source)
    docstrings = _docstring_node_ids(tree)
    findings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            if _SPANISH_HINT.search(node.value):
                findings.append(f"line {node.lineno}: {node.value[:60]!r}")
    return findings


def _python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


# --------------------------------------------------------------------------- #
# The scanners themselves: synthetic sources that must be caught
# --------------------------------------------------------------------------- #
def test_scanner_catches_a_positional_text_argument() -> None:
    """The calling convention actually used in ``handlers.py``: ``self._reply(cid, "Hola")``."""

    source = 'class H:\n    async def f(self):\n        await self._reply(chat_id, "Hola amigo")\n'
    findings = find_literal_text_arguments(source)
    assert len(findings) == 1, findings
    assert "Hola amigo" in findings[0]


def test_scanner_catches_a_keyword_text_argument() -> None:
    source = 'async def f():\n    await send_message(chat_id=1, text="buenas")\n'
    assert find_literal_text_arguments(source)


def test_scanner_catches_an_f_string_in_either_position() -> None:
    positional = 'async def f():\n    await notify(1, f"hola {x}")\n'
    keyword = 'async def f():\n    await send_message(chat_id=1, text=f"hola {x}")\n'
    assert find_literal_text_arguments(positional)
    assert find_literal_text_arguments(keyword)


def test_scanner_accepts_catalogue_rendered_text() -> None:
    source = (
        "async def f():\n"
        "    await send_message(chat_id=1, text=render('start'))\n"
        "    await self._reply(identity.chat_id, render('reset.done', command='/x'))\n"
    )
    assert find_literal_text_arguments(source) == []


def test_scanner_ignores_ids_and_non_senders() -> None:
    source = (
        "async def f():\n"
        "    await send_message(chat_id=chat_id, text=text)\n"
        "    logger.info('a literal that is only a log line')\n"
        "    await self._store.append(1, messages)\n"
    )
    assert find_literal_text_arguments(source) == []


@pytest.mark.parametrize(
    "literal",
    [
        '"No entiendo eso"',  # unaccented Spanish, the case the old detector missed
        '"Vas demasiado rápido"',  # accented
        '"Te lo envío ahora"',
        '"He guardado tu recordatorio"',
        '"¿Quieres que lo repita?"',
    ],
)
def test_scanner_catches_spanish_regardless_of_accents(literal: str) -> None:
    source = f"async def f():\n    x = {literal}\n"
    assert find_spanish_literals(source), literal


def test_scanner_ignores_docstrings_and_english_prose() -> None:
    source = (
        '"""Un módulo que habla de mensajes y usuarios en un docstring."""\n'
        "async def f():\n"
        '    """Otro docstring en español con recordatorios."""\n'
        '    logger.warning("unhandled handler error", extra={"error_code": "boom"})\n'
    )
    assert find_spanish_literals(source) == []


# --------------------------------------------------------------------------- #
# The catalogue
# --------------------------------------------------------------------------- #
def test_catalogue_keys_are_dotted_and_unique() -> None:
    for key in messages.CATALOGUE:
        assert key
        assert " " not in key
    assert len(set(messages.CATALOGUE)) == len(messages.CATALOGUE)


def test_catalogue_values_are_non_empty_and_spanish() -> None:
    for key, value in messages.CATALOGUE.items():
        assert isinstance(value, str) and value.strip(), f"{key} is empty"
        assert find_spanish_literals(f'x = "{value}"'.replace("\n", " ")) or "{" in value or len(
            value
        ) > 30, f"{key} does not look Spanish"

    joined = " ".join(messages.CATALOGUE.values())
    assert "todavía" in joined or "disponible" in joined


def test_catalogue_uses_only_known_placeholders() -> None:
    """Replaces a tautology from the first revision (``if '{command}' in value: assert '{' in
    value``), which asserted nothing. This one actually constrains the catalogue."""

    seen: set[str] = set()
    for value in messages.CATALOGUE.values():
        for placeholder in re.findall(r"\{([a-z_]+)\}", value):
            seen.add(placeholder)
    assert seen <= _ALLOWED_PLACEHOLDERS, f"unknown placeholders in the catalogue: {seen}"


def test_render_substitutes_placeholders() -> None:
    assert "/tasks" in messages.render("error.not_available", command="/tasks")


def test_render_raises_for_an_unknown_key() -> None:
    with pytest.raises(KeyError):
        messages.render("does.not.exist")


def test_has() -> None:
    assert messages.has("reset.done")
    assert not messages.has("nope")


def test_every_render_call_in_the_codebase_uses_a_real_key() -> None:
    used: set[str] = set()
    for path in _python_files(APP_DIR):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and _callee_name(node) == "render"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                used.add(node.args[0].value)

    assert used, "no render() calls found — the scan is broken"
    missing = sorted(key for key in used if not messages.has(key))
    assert missing == [], f"catalogue keys referenced but not defined: {missing}"


def test_error_taxonomy_message_keys_all_exist() -> None:
    from app import errors

    missing: list[str] = []
    for name in errors.__all__:
        candidate = getattr(errors, name)
        if isinstance(candidate, type) and issubclass(candidate, errors.AssistantError):
            if not messages.has(candidate.message_key):
                missing.append(f"{name} -> {candidate.message_key}")
    assert missing == [], f"error message keys missing from the catalogue: {missing}"


# --------------------------------------------------------------------------- #
# The rules applied to the real tree
# --------------------------------------------------------------------------- #
def test_no_literal_text_is_sent_to_telegram() -> None:
    """Text reaches the user only through ``render(...)``, in any argument position."""

    violations: list[str] = []
    for path in _python_files(APP_DIR):
        if path.name in _ALLOWED_SPANISH:
            continue
        source = path.read_text(encoding="utf-8")
        for finding in find_literal_text_arguments(source):
            violations.append(f"{path.relative_to(APP_DIR)}: {finding}")
    assert violations == [], f"text sent without the catalogue: {violations}"


def test_no_spanish_literals_outside_the_catalogue() -> None:
    """The whole application tree, not just ``bot/`` and ``api/`` (review item 6)."""

    violations: list[str] = []
    for path in _python_files(APP_DIR):
        if path.name in _ALLOWED_SPANISH:
            continue
        source = path.read_text(encoding="utf-8")
        for finding in find_spanish_literals(source):
            violations.append(f"{path.relative_to(APP_DIR)}: {finding}")
    assert violations == [], f"Spanish literal outside app/bot/messages.py: {violations}"
