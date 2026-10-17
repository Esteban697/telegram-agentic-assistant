"""The import matrix of PLAN.md §2.2, enforced mechanically.

Eight layers; a module may import only from lower-numbered layers. Nothing in this file
imports the application — it parses it — so it needs no credentials and no wiring, which is
what lets the layering rule hold from Phase 1 onward (decision 14: a stdlib ``ast`` walk
instead of an import-lint dependency).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[2] / "app"


_PACKAGE_LAYERS = {
    "policy": 0,
    "llm": 1,
    "storage": 2,
    "services": 3,
    "tools": 4,
    "agent": 5,
    "bot": 6,
    "api": 7,
    "jobs": 8,
}


def _layer_of(relative: Path) -> int:
    """Map a module path to its layer number, per §2.2."""

    parts = relative.parts
    name = relative.name

    if len(parts) == 1:  # top-level modules
        if name in {"config.py", "logging_config.py", "errors.py"}:
            return 0
        if name == "main.py":
            return 8
        pytest.fail(f"unclassified top-level module: {relative}")

    package = parts[0]
    if package == "policy":
        return 0
    if package == "llm":
        return 0 if name in {"protocols.py", "errors.py"} else 1
    if package == "storage":
        return 1 if name in {"postgres.py", "models.py", "gcs.py"} else 2
    if package in _PACKAGE_LAYERS:
        return _PACKAGE_LAYERS[package]
    pytest.fail(f"unclassified module: {relative}")


def _layer_of_dotted(dotted: str) -> int:
    """Layer of an imported dotted path, including a bare package import."""

    remainder = dotted[len("app.") :]
    segments = remainder.split(".")
    if len(segments) == 1:
        # `from app.bot import messages` arrives as "app.bot": a package-level import.
        return _PACKAGE_LAYERS[segments[0]]
    return _layer_of(Path(*segments))


def _imported_app_modules(path: Path) -> set[str]:
    """Return the dotted ``app.*`` modules imported by a file, including relative imports."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()

    package_parts = list(path.relative_to(APP_DIR).parts[:-1])
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app."):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import: resolve against this file's package
                base = package_parts[: len(package_parts) - (node.level - 1)]
                target = ".".join([*base, *(node.module.split(".") if node.module else [])])
                found.add(f"app.{target}" if not target.startswith("app") else target)
            elif node.module and node.module.startswith("app."):
                found.add(node.module)
    return found


def _layer_of_dotted(dotted: str) -> int:
    """Layer of an imported dotted path, including a bare package **or** module import."""

    remainder = dotted[len("app.") :]
    segments = remainder.split(".")
    if len(segments) == 1:
        # `from app.bot import messages` arrives as "app.bot" (a package), while
        # `import app.errors` arrives as "app.errors" (a top-level module).
        if segments[0] in _PACKAGE_LAYERS:
            return _PACKAGE_LAYERS[segments[0]]
        return _layer_of(Path(f"{segments[0]}.py"))
    return _layer_of(Path(*segments))


def _app_files() -> list[Path]:
    return sorted(
        path
        for path in APP_DIR.rglob("*.py")
        if "__pycache__" not in path.parts and path.name != "__init__.py"
    )


@pytest.mark.parametrize("path", _app_files(), ids=lambda p: str(p.name))
def test_no_module_imports_a_higher_layer(path: Path) -> None:
    relative = path.relative_to(APP_DIR)
    own_layer = _layer_of(relative)

    violations: list[str] = []
    for dotted in _imported_app_modules(path):
        target_layer = _layer_of_dotted(dotted)
        if target_layer > own_layer:
            violations.append(f"{relative} (layer {own_layer}) imports {dotted} (layer {target_layer})")
    assert violations == [], "; ".join(violations)


def test_agent_never_imports_provider_code_or_transport() -> None:
    """The rule the plan spells out: provider calls live behind ``llm/`` and ``tools/``."""

    banned = {"httpx", "app.llm.ollama_client", "app.llm.embeddings", "fastapi"}
    violations: list[str] = []
    for path in (APP_DIR / "agent").rglob("*.py"):
        for dotted in _imported_app_modules(path) | _top_level_imports(path):
            if dotted in banned:
                violations.append(f"{path.name} imports {dotted}")
    assert violations == [], "; ".join(violations)


def test_agent_never_imports_the_stores() -> None:
    """§2.2 / §10 Phase 1 deliverable 9: the handler assembles the prompt, not the graph."""

    violations: list[str] = []
    for path in (APP_DIR / "agent").rglob("*.py"):
        for dotted in _imported_app_modules(path) | _top_level_imports(path):
            if dotted.startswith("app.storage"):
                violations.append(f"{path.name} imports {dotted}")
    assert violations == [], "; ".join(violations)


def test_services_never_import_fastapi_or_telegram() -> None:
    services = APP_DIR / "services"
    if not services.exists():  # Phase 1 has no services package yet
        pytest.skip("no services package in Phase 1")
    violations: list[str] = []
    for path in services.rglob("*.py"):
        for dotted in _imported_app_modules(path) | _top_level_imports(path):
            if dotted in {"fastapi", "app.bot.telegram_client"}:
                violations.append(f"{path.name} imports {dotted}")
    assert violations == [], "; ".join(violations)


def test_storage_never_imports_services() -> None:
    violations: list[str] = []
    for path in (APP_DIR / "storage").rglob("*.py"):
        for dotted in _imported_app_modules(path):
            if dotted.startswith("app.services"):
                violations.append(f"{path.name} imports {dotted}")
    assert violations == [], "; ".join(violations)


def test_only_the_wiring_module_constructs_provider_clients() -> None:
    """``main.py`` is the only place concrete implementations are chosen (§10 deliverable 11)."""

    violations: list[str] = []
    for path in _app_files():
        if path.name == "main.py" or path.parent.name == "llm":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name == "OllamaChatClient":
                    violations.append(f"{path.relative_to(APP_DIR)}:{node.lineno}")
    assert violations == [], f"OllamaChatClient constructed outside main.py/llm: {violations}"


def _top_level_imports(path: Path) -> set[str]:
    """Return third-party/stdlib top-level module names imported by a file."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if not node.level and node.module:
                found.add(node.module.split(".")[0])
    return found
