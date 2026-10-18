"""README accuracy guards.

The README is the product document: it describes the assistant and how to run it, not the
project's phase log. A claim in it that the code does not support is a defect, not a style issue,
so these tests pin the statements that matter and prevent the wording from drifting back.
"""

from __future__ import annotations

from pathlib import Path

README = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")


def test_readme_describes_persistent_state_without_overclaiming() -> None:
    """State lives in PostgreSQL through LangGraph's AsyncPostgresSaver.

    The README must not promise that the graph is untouched by persistence, and must name the
    real checkpointer instead of describing an in-memory-only design.
    """

    assert "sin tocar el grafo" not in README
    assert "AsyncPostgresSaver" in README


def test_readme_documents_the_telegram_429_limitation() -> None:
    """The reply and the turn are dropped on a 429; that must be stated, not discovered."""

    assert "429" in README
    assert "retry_after" in README
    assert "la respuesta se pierde" in README


def test_readme_documents_the_local_only_mode() -> None:
    assert "solo local" in README
    assert "STORE_BACKEND=memory" in README


def test_readme_is_product_documentation_not_a_phase_log() -> None:
    """The README describes the assistant; phase status belongs in PLAN.md and the reports."""

    for phase in ("Fase 1", "Fase 2", "Fase 3", "Fase 4", "Fase 5", "Fase 6"):
        assert phase not in README
