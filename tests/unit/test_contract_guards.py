"""Guards that walk **every** module under ``app/`` (Phase 2 review items 3, 5, 6).

The first version of the vector-chokepoint test scanned only ``repositories.py``, so a second
retrieval path added later in ``app/tools/documents.py`` would not have been caught. These tests
parse the whole tree, and each of them fails against the code as it stood before the review.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "app"

VECTOR_DISTANCE_ATTRS = frozenset({"cosine_distance", "l2_distance", "max_inner_product"})
SQL_CALLS = frozenset({"text", "execute", "exec_driver_sql", "from_statement", "literal_column"})


def _modules() -> list[Path]:
    return sorted(
        path for path in APP.rglob("*.py") if "__pycache__" not in path.parts
    )


def _functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _callee_name(node: ast.Call) -> str:
    function = node.func
    if isinstance(function, ast.Attribute):
        return function.attr
    return getattr(function, "id", "")


# --------------------------------------------------------------------------- #
# item 6: the vector chokepoint, over the whole tree
# --------------------------------------------------------------------------- #
def test_only_search_chunks_issues_a_vector_distance_ordering() -> None:
    """PLAN.md §6.5: exactly one function may order by vector distance."""

    offenders: list[str] = []
    for module in _modules():
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in _functions(tree):
            if node.name == "search_chunks":
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Attribute) and inner.attr in VECTOR_DISTANCE_ATTRS:
                    offenders.append(f"{module.relative_to(REPO)}::{node.name}")
    assert offenders == [], f"vector ordering outside search_chunks: {offenders}"


def test_the_chokepoint_scan_actually_would_catch_a_second_path(tmp_path: Path) -> None:
    """A guard that cannot fail is worthless: prove the detector fires on a planted offender."""

    planted = tmp_path / "documents.py"
    planted.write_text(
        "import sqlalchemy as sa\n"
        "async def sneaky_search(session, embedding):\n"
        "    return await session.execute(\n"
        "        sa.select(sa.func.max(sa.text('embedding')).op('<=>')(embedding))\n"
        "        .order_by(Chunk.embedding.cosine_distance(embedding))\n"
        "    )\n",
        encoding="utf-8",
    )
    tree = ast.parse(planted.read_text(encoding="utf-8"))
    found = [
        inner.attr
        for node in _functions(tree)
        for inner in ast.walk(node)
        if isinstance(inner, ast.Attribute) and inner.attr in VECTOR_DISTANCE_ATTRS
    ]
    assert found == ["cosine_distance"], "the detector must find a planted second retrieval path"


# --------------------------------------------------------------------------- #
# item 5: §7.3's guard, which Phase 2 promised and did not have
# --------------------------------------------------------------------------- #
def test_no_dynamic_sql_is_built_by_interpolation() -> None:
    """No f-string, `%`, `+` or `.format()` may reach SQL execution.

    `reconcile_stale_dispatches` built ``sa.text(f"interval '{int(lease_seconds)} seconds'")``.
    It was not injectable — the value is a validated int — but it is exactly the shape that
    becomes injectable the day someone threads a string through it, and §7.3 says the codebase
    must not contain it.
    """

    offenders: list[str] = []
    for module in _modules():
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _callee_name(node) not in SQL_CALLS:
                continue
            for argument in node.args:
                if isinstance(argument, ast.JoinedStr):
                    offenders.append(f"{module.name}:{node.lineno} f-string into {argument!r}"[:120])
                elif isinstance(argument, ast.BinOp) and isinstance(argument.op, (ast.Mod, ast.Add)):
                    offenders.append(f"{module.name}:{node.lineno} %/+ concatenation")
                elif (
                    isinstance(argument, ast.Call)
                    and isinstance(argument.func, ast.Attribute)
                    and argument.func.attr == "format"
                ):
                    offenders.append(f"{module.name}:{node.lineno} .format()")
                elif isinstance(argument, ast.Call) and _callee_name(argument) == "f":
                    offenders.append(f"{module.name}:{node.lineno} f() helper")
    assert offenders == [], f"string-built SQL under app/: {offenders}"


def test_the_sql_guard_fires_on_the_shape_it_forbids(tmp_path: Path) -> None:
    """Prove the guard can fail, using the exact expression that was removed."""

    planted = tmp_path / "bad.py"
    planted.write_text(
        "import sqlalchemy as sa\n"
        "def build(lease_seconds):\n"
        "    return sa.text(f\"interval '{int(lease_seconds)} seconds'\")\n",
        encoding="utf-8",
    )
    tree = ast.parse(planted.read_text(encoding="utf-8"))
    offences = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _callee_name(node) in SQL_CALLS
        and any(isinstance(argument, ast.JoinedStr) for argument in node.args)
    ]
    assert offences, "the guard must flag an f-string passed to sa.text()"


def test_the_stale_lease_comparison_is_parameterised() -> None:
    """The specific expression that was replaced, asserted structurally rather than by text.

    A source-text search would trip over the comment that documents the fix, so this looks at
    the AST: `reconcile_stale_dispatches` must build its interval with `make_interval` (a bound
    parameter) and must not pass an f-string to any SQL call — the latter is also covered
    tree-wide by the guard above.
    """

    source = (APP / "storage" / "repositories.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in _functions(tree):
        if node.name != "reconcile_stale_dispatches":
            continue
        attributes = {
            inner.attr for inner in ast.walk(node) if isinstance(inner, ast.Attribute)
        }
        assert "make_interval" in attributes, (
            "the lease interval must be a bound parameter, not interpolated text"
        )
        f_strings = [
            inner
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call)
            and _callee_name(inner) in SQL_CALLS
            and any(isinstance(argument, ast.JoinedStr) for argument in inner.args)
        ]
        assert f_strings == [], "no f-string may reach a SQL call"
        return
    raise AssertionError("reconcile_stale_dispatches not found")


# --------------------------------------------------------------------------- #
# item 3: no frozen parameter that cannot do anything
# --------------------------------------------------------------------------- #
def test_search_chunks_declares_no_parameter_it_does_not_use() -> None:
    """`pipeline_version` was accepted and never read: a trap in a frozen contract."""

    tree = ast.parse((APP / "storage" / "repositories.py").read_text(encoding="utf-8"))
    for node in _functions(tree):
        if node.name != "search_chunks":
            continue
        declared = {argument.arg for argument in node.args.args}
        declared |= {argument.arg for argument in node.args.kwonlyargs}
        body = ast.get_source_segment(
            (APP / "storage" / "repositories.py").read_text(encoding="utf-8"), node
        ) or ""
        unused = [
            name
            for name in declared
            if name not in {"self", "session", "cls"} and body.count(name) <= 1
        ]
        assert unused == [], f"search_chunks declares unused parameters: {unused}"
        return
    raise AssertionError("search_chunks not found")
