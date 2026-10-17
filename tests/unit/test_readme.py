"""README accuracy guards (review items 9a/9b).

The README is a spec deliverable (§18.7) and A9 is demonstrated by following it, so a claim in
it that the code does not support is a defect, not a style issue. These tests are deliberately
narrow: they pin the two statements the Phase 1 review found inaccurate, so the wording cannot
drift back without a deliberate edit here.
"""

from __future__ import annotations

from pathlib import Path

README = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")


def test_readme_does_not_claim_the_graph_is_untouched() -> None:
    """Attaching a checkpointer *does* edit ``agent/graph.py``.

    The plan's wording is "Phase 2 only has to attach the checkpointer rather than
    restructure"; the earlier README promised the store could be swapped "sin tocar el grafo",
    which the layering rule does not guarantee.
    """

    assert "sin tocar el grafo" not in README
    assert "adjuntar el checkpointer" in README


def test_readme_documents_the_telegram_429_limitation() -> None:
    """The reply and the turn are dropped on a 429; that must be stated, not discovered."""

    assert "429" in README
    assert "retry_after" in README
    assert "la respuesta se pierde" in README


def test_readme_states_phase_1_is_local_only() -> None:
    assert "solo local" in README
    assert "STORE_BACKEND=memory" in README
