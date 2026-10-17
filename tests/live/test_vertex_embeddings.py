"""Live tests: real Vertex AI embeddings (PLAN.md §13.1).

`tests/live/conftest.py` applies the `live` marker to every module in this directory, so nothing
here can be collected by an unfiltered run.

**Skipped unless `RUN_LIVE_TESTS=1`.** They need Application Default Credentials and the Vertex AI
API enabled:

    gcloud auth application-default login
    gcloud services enable aiplatform.googleapis.com
    RUN_LIVE_TESTS=1 uv run pytest -m live

These are the tests that would close Phase 4 acceptance (a) and (b): the stub cannot demonstrate
that the right *page* is retrieved for a real question, nor the no-evidence branch on a realistic
corpus, because both are retrieval-quality claims.
"""

from __future__ import annotations

import os

import pytest

from app.llm.embeddings import VertexEmbeddingProvider, cosine_similarity
from app.services.document_ingestion import extract_pages

pytestmark = [pytest.mark.live, pytest.mark.asyncio]


def _require_credentials() -> None:
    """Skip *inside* the test, never at import time.

    The probe used to run as a module-level `skipif` argument, so merely collecting this file
    called `google.auth.default()`. That is credential coupling leaking into the default suite:
    it degraded to a skip, but the default run still touched the environment it promises not to
    touch.
    """

    if os.environ.get("RUN_LIVE_TESTS") != "1":
        pytest.skip("set RUN_LIVE_TESTS=1 and run `gcloud auth application-default login`")
    try:
        import google.auth

        google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    except Exception as exc:  # noqa: BLE001 - any ADC failure means the live path cannot run
        pytest.skip(f"no Application Default Credentials: {type(exc).__name__}")


requires_live = pytest.mark.live


@requires_live
async def test_a_vertex_embeddings_have_the_configured_dimension() -> None:
    _require_credentials()
    provider = VertexEmbeddingProvider(
        project=os.environ["GOOGLE_CLOUD_PROJECT"],
        location=os.environ.get("GOOGLE_CLOUD_REGION", "europe-west1"),
        dim=768,
    )
    try:
        vectors = await provider.embed(["La renta mensual es de 950 euros."])
    finally:
        await provider.aclose()

    assert len(vectors) == 1
    assert len(vectors[0]) == 768, "the vector(768) column depends on this"


@requires_live
async def test_a_and_b_a_known_page_is_retrieved_and_unrelated_text_is_not() -> None:
    """What the deployed provider can and cannot establish, measured with the configured floor.

    * **(a)** the page that answers the question ranks first.
    * **(b)** the no-evidence branch: the honest claim is *not* "an unrelated question scores below
      the floor". Measured here, it does not — 0.5425 against 0.7332 for the answering page — and
      Vertex compresses "unrelated" and "paraphrase" into one band, so a threshold between them
      would also reject legitimate paraphrases. What this test asserts is the relationship the
      shipping code and `app/config.py` both depend on, **using `settings.rag_min_score` instead of
      a free-floating constant**: an unrelated question clears the floor (so the floor is a
      degenerate-match filter and the prompt does the judging), and it still scores below the
      answering page. If either stops holding, the measurement in `app/config.py` is stale and the
      test says so.

    The earlier version asserted `best_unrelated < 0.75`, a constant that appears nowhere in the
    system, so it passed a configuration whose no-evidence branch was unreachable — and PLAN.md
    recorded 0.75 as "the threshold the no-evidence branch depends on", which was never true.
    """

    _require_credentials()

    from app.config import Settings
    from app.tools.documents import classify_evidence

    floor = Settings().rag_min_score
    provider = VertexEmbeddingProvider(
        project=os.environ["GOOGLE_CLOUD_PROJECT"],
        location=os.environ.get("GOOGLE_CLOUD_REGION", "europe-west1"),
        dim=768,
    )
    try:
        pages = extract_pages(_fixture_bytes(), "text/plain")
        passages = [page.text for page in pages if page.text.strip()]
        assert len(passages) >= 2, "precondition: a page-level claim needs more than one page"

        # The two sides of a search are embedded differently, and mixing them up is a silent
        # quality loss: the question as a query, the passages as documents.
        (question_vector,) = await provider.embed(
            ["¿Cuál es el plazo de preaviso para dejar la vivienda?"], task="query"
        )
        document_vectors = await provider.embed(passages)
        (unrelated_vector,) = await provider.embed(
            ["Receta de tortilla con patatas"], task="query"
        )
    finally:
        await provider.aclose()

    scores = [
        cosine_similarity(question_vector, vector) for vector in document_vectors
    ]
    best = max(range(len(document_vectors)), key=lambda index: scores[index])
    # The answer lives on the page that mentions the notice period; the fixture is small enough
    # that the page index is the assertion, exactly as the acceptance criterion states it.
    print(f"VERTEX (a): best page index {best} of {len(passages)} (score {scores[best]:.4f})")
    assert "preaviso" in passages[best].lower(), (
        f"the wrong page was retrieved: index {best} of {len(passages)}"
    )

    # (b): the score an unrelated question reaches against the same pages.
    best_unrelated = max(
        cosine_similarity(unrelated_vector, vector) for vector in document_vectors
    )
    print(
        f"VERTEX (b): unrelated {best_unrelated:.4f} vs answering page {scores[best]:.4f} "
        f"(configured floor {floor})"
    )
    assert best_unrelated < scores[best], (
        "an unrelated question outranked the page that answers a real question"
    )
    assert best_unrelated >= floor, (
        f"the configured floor ({floor}) is at or above an unrelated question's score "
        f"({best_unrelated:.4f}). That would make the floor a relevance claim — and the paraphrase "
        "cost of a floor that high is measured in app/config.py (a rephrased question whose answer "
        "IS in the corpus scores 0.25-0.28 with the stub). Re-measure before changing it."
    )
    assert classify_evidence(best_unrelated, floor=floor) == "matches", (
        "the code no longer classifies an unrelated match the way app/config.py documents"
    )


async def _embed(
    provider: VertexEmbeddingProvider, texts: list[str], *, task: str = "document"
) -> list[list[float]]:
    return await provider.embed(texts, task=task)


def _fixture_bytes() -> bytes:
    """The tiny committed fixture, whose notice period is on its second page."""

    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "fixtures" / "contrato_alquiler.txt"
    if not path.exists():  # pragma: no cover - the fixture is generated by the eval suite
        pytest.skip(f"fixture missing: {path}")
    return path.read_bytes()
