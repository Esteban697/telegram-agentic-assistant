"""Phase 4 guards that must run in the **default** suite.

Three things live here because they are cheap, credential-free, and were previously (or would
otherwise be) asserted in files that skip:

1. **The pending-guard test.** ``test_a_and_b_are_marked_pending_not_passing`` used to live in
   ``tests/integration/test_phase4_documents.py``, which is DB-marked and therefore skipped
   without ``TEST_DATABASE_URL`` — a test whose whole job is to keep an honest claim honest was
   itself not running. It needs no database, so it belongs here. This is the fourth appearance of
   the "a green suite that never exercises its own guard" pattern, so the guard now has its own
   guard: the checker is a function, and two tests below prove it can fail.
2. ``sanitize_filename`` against a table of traversal variants.
3. The same, as a property: no output may contain a ``..`` segment.

Nothing here reads the network, a credential or the clock.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services.document_ingestion import sanitize_filename

LIVE_TEST = Path(__file__).resolve().parents[1] / "live" / "test_vertex_embeddings.py"


# --------------------------------------------------------------------------- #
# 1. The pending-guard test, now in the default suite
# --------------------------------------------------------------------------- #
def check_live_guard_file(path: Path) -> str:
    """Return the live test's source, or explain why the pending claim is unsupported.

    Raises ``AssertionError`` when the file is missing (the pending claim would then point at
    nothing) or when the file lost its opt-in gate (it would run by default and turn a
    credential-dependent criterion into an apparent pass).
    """

    assert path.exists(), (
        f"the live test that closes (a)/(b) must exist: {path} is missing. The pending claim "
        "would point at nothing."
    )
    source = path.read_text(encoding="utf-8")
    assert "RUN_LIVE_TESTS" in source, (
        f"{path.name} must be opt-in: without RUN_LIVE_TESTS it would run in the default suite "
        "and a credential-dependent criterion would look demonstrated"
    )
    assert "google.auth" in source or "build_embedding_provider" in source, (
        f"{path.name} no longer mentions the real provider path, so it is not closing (a)/(b)"
    )
    assert "pytest.mark.live" in source or "mark.live" in source, (
        f"{path.name} must carry the live marker so `-m 'not live'` selects it out"
    )
    return source


def test_a_and_b_are_marked_pending_not_passing() -> None:
    """The plan's honest framing, asserted so it cannot be quietly upgraded.

    A stub cannot demonstrate that the *right page* is retrieved for a real question, nor the
    no-evidence branch on a realistic corpus: both are retrieval-quality claims. The live test
    that would close them is `tests/live/test_vertex_embeddings.py`, which skips without ADC.
    """

    check_live_guard_file(LIVE_TEST)


def test_the_live_guard_checker_fails_when_the_file_is_missing(tmp_path: Path) -> None:
    """Proof that the guard above can fail: a deleted live file is caught, not ignored."""

    with pytest.raises(AssertionError, match="must exist"):
        check_live_guard_file(tmp_path / "test_vertex_embeddings.py")


def test_the_live_guard_checker_fails_when_the_opt_in_gate_is_removed(tmp_path: Path) -> None:
    """Proof that the guard above can fail: a file that lost its gate is caught."""

    stripped = tmp_path / "test_vertex_embeddings.py"
    source = LIVE_TEST.read_text(encoding="utf-8")
    stripped.write_text(source.replace("RUN_LIVE_TESTS", "SOMETHING_ELSE"), encoding="utf-8")

    with pytest.raises(AssertionError, match="opt-in"):
        check_live_guard_file(stripped)


# --------------------------------------------------------------------------- #
# 2. sanitize_filename: the traversal table
# --------------------------------------------------------------------------- #
TRAVERSAL_TABLE = [
    # (input, expected leaf name). The expectations are the implementation's real output, checked
    # by hand for safety: no separator, no `..` run, no leading dot. `..` runs collapse to `_`
    # (which is why `a/../../b` keeps three underscores: two separators and one collapsed run).
    ("../../etc/passwd", "etc_passwd"),
    ("../../../etc/shadow", "etc_shadow"),
    ("..\\..\\windows\\system32\\cmd.exe", "windows_system32_cmd.exe"),
    ("sub/../..//nested.txt", "sub_____nested.txt"),
    ("a/../../b", "a_____b"),
    ("..foo/../bar.txt", "foo___bar.txt"),
    ("%2e%2e%2fetc%2fpasswd", "2e_2e_2fetc_2fpasswd"),
    ("..%2f..%2fetc%2fpasswd", "2f__2fetc_2fpasswd"),
    ("....", "documento"),
    ("..", "documento"),
    ("...hidden..txt", "hidden_txt"),
    ("file..name..txt", "file_name_txt"),
    ("trailing...", "trailing"),
    ("  spaced name.txt  ", "spaced_name.txt"),
    ("", "documento"),
    ("/", "documento"),
    ("a/b", "a_b"),
    ("x..y//z", "x_y_z"),
]


@pytest.mark.parametrize(("raw", "expected"), TRAVERSAL_TABLE, ids=[t[0] or "<empty>" for t in TRAVERSAL_TABLE])
def test_sanitize_filename_handles_traversal_variants(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


@pytest.mark.parametrize(("raw", "_expected"), TRAVERSAL_TABLE, ids=[t[0] or "<empty>" for t in TRAVERSAL_TABLE])
def test_sanitize_filename_never_leaves_a_parent_directory_segment(raw: str, _expected: str) -> None:
    """**The property that fails if the `..` collapsing is removed.**

    ``a/../../b`` sanitises to ``a_b`` only because interior ``..`` runs are collapsed; without
    that loop the result is ``a_.._.._b``, which is a traversal-shaped object key.
    """

    sanitized = sanitize_filename(raw)
    assert ".." not in sanitized, f"{raw!r} sanitised to {sanitized!r}, which still contains '..'"
    assert "/" not in sanitized and "\\" not in sanitized
    assert not sanitized.startswith(".")
    assert sanitized.strip() == sanitized


def test_sanitize_filename_property_check_can_fail() -> None:
    """Proof the property above is not vacuous: it rejects a value that keeps a `..` segment."""

    uncollapsed = re.sub(r"[^A-Za-z0-9._ -]", "_", "a/../../b").strip("._")
    assert ".." in uncollapsed, "precondition: the un-collapsed form must keep the '..'"
    assert uncollapsed != sanitize_filename("a/../../b")


def test_sanitize_filename_truncates_and_stays_a_leaf() -> None:
    long_name = ("a" * 300) + "/" + ("b" * 300) + ".txt"
    sanitized = sanitize_filename(long_name)
    assert len(sanitized) <= 120
    assert "/" not in sanitized


# --------------------------------------------------------------------------- #
# 3. Every tool handler must accept the registry's calling convention
# --------------------------------------------------------------------------- #
def _all_tool_specs():  # noqa: ANN202 - a list of ToolSpec
    """Every handler the registry can call, built without a database or a network.

    ``sessions=None`` is fine: nothing here executes a handler, it inspects one.
    """

    from app.tools.documents import DocumentTools
    from app.tools.documents import specs as document_specs
    from app.tools.billing import BillingTool
    from app.tools.billing import specs as billing_specs
    from app.tools.calendar import CalendarTools
    from app.tools.calendar import specs as calendar_specs
    from app.tools.clock import ClockTool
    from app.tools.clock import specs as clock_specs
    from app.tools.memory import MemoryTools
    from app.tools.memory import specs as memory_specs
    from app.tools.restaurants import RestaurantTools
    from app.tools.restaurants import specs as restaurant_specs
    from app.tools.tasks import TaskTools
    from app.tools.tasks import specs as task_specs
    from app.tools.web_search import WebSearchTool
    from app.tools.web_search import specs as web_specs

    tools = [
        *memory_specs(MemoryTools(sessions=None)),
        *task_specs(TaskTools(sessions=None)),
        *web_specs(WebSearchTool()),
        *restaurant_specs(RestaurantTools()),
        *document_specs(DocumentTools(sessions=None, embeddings=None, min_score=0.1)),
        *clock_specs(ClockTool()),
        *calendar_specs(CalendarTools(provider=None)),
        *billing_specs(BillingTool()),
    ]
    assert len(tools) >= 10, "precondition: the tool surface must not shrink silently"
    return tools


@pytest.mark.parametrize("spec", _all_tool_specs(), ids=lambda spec: spec.name)
def test_every_tool_handler_accepts_the_registry_calling_convention(spec) -> None:
    """``ToolRegistry._call`` invokes ``handler(user_id=..., args=..., context=...)``.

    Seven handlers (four in ``MemoryTools``, three in ``TaskTools``) were declared without the
    trailing ``**_``, so calling them *through the registry* — the only way production calls them
    — raised ``TypeError: got an unexpected keyword argument 'context'``. Every memory and task
    tool was unusable, and no test noticed because the Phase 3 tests called the handlers directly.
    This guard binds the signature the registry uses, for every spec, in the default suite.
    """

    import inspect

    args = spec.args_model.model_construct()
    inspect.signature(spec.handler).bind(user_id=1, args=args, context={})


def test_the_handler_contract_guard_can_fail() -> None:
    """Proof the guard is not vacuous: a handler without `**_` is rejected by it."""

    import inspect

    async def handler(*, user_id: int, args: object) -> None:  # noqa: ARG001
        return None

    with pytest.raises(TypeError):
        inspect.signature(handler).bind(user_id=1, args=object(), context={})
