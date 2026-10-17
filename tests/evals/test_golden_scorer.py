"""Mode 1 of PLAN.md §9.3: the *scorer* is tested against recorded transcripts.

**No database, no credentials, no network.** This is the half that protects the evaluations from
rotting: if the scorer stops being able to fail, or the golden file loses the ordering it is
supposed to carry, the failure shows up here rather than as a page evaluation that quietly passes
on any ranking at all.

It exists because it did not: the page-citation evaluation was deleted in the previous pass and
`golden_expectations`, `load_golden`, `transcript_of` and `score_transcript` were imported into
`test_rag_evals.py` and **never called**. Grepping for call sites found none, the golden file
recorded one citation per question, and no test in the suite asserted the correct document or the
correct page for any query.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.evals.conftest import (
    Expectation,
    golden_expectations,
    load_golden,
    score_transcript,
    transcript_of,
)

pytestmark = pytest.mark.eval

GOLDEN_FILE = "retrieval_expected.json"


def _fixture() -> tuple[dict[str, Any], list[Expectation], list[dict[str, Any]]]:
    golden = load_golden(GOLDEN_FILE)
    expectations = golden_expectations(golden)
    return golden, expectations, transcript_of(golden)


def _multi(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [payload for payload in payloads if len(payload["citations"]) >= 2]


# --------------------------------------------------------------------------- #
# The fixture itself
# --------------------------------------------------------------------------- #
def test_the_golden_records_ordering_past_rank_one() -> None:
    """The defect B3 names: one citation per question makes a reversed ranking unrepresentable."""

    golden, expectations, payloads = _fixture()

    assert len(expectations) >= 9, "the golden set shrank"
    multi = _multi(payloads)
    assert len(multi) >= 4, (
        f"only {len(multi)} question(s) record more than one citation, so ordering past rank 1 "
        "cannot be asserted and a shuffled ranking would still pass"
    )
    for entry in golden["questions"]:
        if not entry["found"]:
            assert entry["citations"] == []
            continue
        assert entry["citations"], f"{entry['query']!r} records no citation at all"
        assert entry["citations"][0] == [entry["document"], entry["page"]], (
            f"{entry['query']!r}: rank 1 in `citations` must match `document`/`page`"
        )


# --------------------------------------------------------------------------- #
# The scorer, on the recorded transcript
# --------------------------------------------------------------------------- #
def test_the_recorded_golden_transcript_scores_perfectly() -> None:
    """The reference: a correct retrieval scores 1.0, so the negative cases below mean something."""

    _, expectations, payloads = _fixture()
    score = score_transcript(expectations, payloads)

    assert score.accuracy == 1.0, score.report()
    assert score.misses == 0


def test_a_reversed_ranking_fails_every_question_it_can_affect() -> None:
    """**The assertion the deleted evaluation could not make.**

    Reversing the citation list leaves rank 1 wrong on every multi-citation question, so a system
    that ordered its sources backwards must fail. If this passes, the page evaluation is a
    decoration.
    """

    _, expectations, payloads = _fixture()
    reversed_payloads = [
        {**payload, "citations": list(reversed(payload["citations"]))} for payload in payloads
    ]

    score = score_transcript(expectations, reversed_payloads)

    affected = len(_multi(payloads))
    assert affected >= 4, "precondition: reversal has to be able to change something"
    assert score.misses >= affected, (
        f"a reversed ranking scored {score.accuracy:.2f}: {score.report()}"
    )
    assert score.accuracy < 1.0


def test_a_rotated_ranking_fails() -> None:
    """A shuffle that keeps the *set* of citations but changes their order must fail too."""

    _, expectations, payloads = _fixture()
    rotated = [
        {**payload, "citations": [*payload["citations"][1:], *payload["citations"][:1]]}
        for payload in payloads
    ]

    score = score_transcript(expectations, rotated)

    assert score.accuracy < 1.0, score.report()
    assert score.misses >= len(_multi(payloads))


def test_a_fabricated_page_fails() -> None:
    """§15 "no inventa páginas": a citation for a page that was never retrieved is a miss."""

    _, expectations, payloads = _fixture()
    fabricated = []
    for payload in payloads:
        if not payload["citations"]:
            fabricated.append(payload)
            continue
        citations = [dict(citation) for citation in payload["citations"]]
        citations[0]["page_number"] = 99
        fabricated.append({**payload, "citations": citations})

    score = score_transcript(expectations, fabricated)

    assert score.misses >= 9, f"a fabricated page scored {score.accuracy:.2f}: {score.report()}"
    assert "page" in score.report()


def test_the_wrong_document_fails() -> None:
    """The claim the first review tested by hand: a wrong-document transcript must not pass."""

    _, expectations, payloads = _fixture()
    wrong = []
    for payload in payloads:
        if not payload["citations"]:
            wrong.append(payload)
            continue
        citations = [dict(citation) for citation in payload["citations"]]
        citations[0]["document_name"] = "otro_documento.txt"
        wrong.append({**payload, "citations": citations})

    score = score_transcript(expectations, wrong)

    assert score.misses >= 9, score.report()
    assert score.accuracy < 0.5


def test_claiming_evidence_for_the_no_evidence_question_is_a_miss() -> None:
    """The other direction: accepting evidence where the golden says there is none."""

    _, expectations, payloads = _fixture()
    invented = [
        {
            **payload,
            "found": True,
            "citations": [
                {
                    "number": 1,
                    "document_name": "contrato_alquiler.txt",
                    "page_number": 1,
                    "document_id": "00000000-0000-0000-0000-000000000000",
                    "score": 0.9,
                }
            ],
        }
        if not payload["citations"]
        else payload
        for payload in payloads
    ]

    score = score_transcript(expectations, invented)

    assert score.misses == 1, score.report()
    assert "claimed evidence" in score.report()


def test_retrieving_nothing_for_an_answerable_question_is_a_miss() -> None:
    """And the plainest failure: a question with a known answer that retrieved nothing."""

    _, expectations, payloads = _fixture()
    empty = [
        {**payload, "found": False, "citations": []} if payload["citations"] else payload
        for payload in payloads
    ]

    score = score_transcript(expectations, empty)

    assert score.misses == len(_fixture()[2]) - 1, score.report()
    assert "retrieved nothing" in score.report()
