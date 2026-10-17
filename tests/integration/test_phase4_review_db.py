"""Phase 4 second pass: the database-backed fixes, each with the test that fails without it.

    TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:55432/taa   uv run pytest -m integration
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from app.llm.embeddings import EmbeddingUnavailable, LexicalStubEmbeddingProvider
from app.services.document_ingestion import DocumentIngestion
from app.storage import repositories
from app.storage.gcs import InMemoryObjectStore
from app.storage.postgres import build_database
from app.tools.documents import DocumentTools, SearchDocumentsArgs
from tests.conftest import TEST_DATABASE_URL, make_db_settings, truncate_all

OWNER = 42

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set"),
]

CONTRACT = (
    "CONTRATO DE ALQUILER\n\nLa renta mensual es de 950 euros.\n\n"
    "La fianza depositada fue de dos mensualidades.\n"
).encode()
QUESTION = {"query": "¿De cuánto fue la fianza depositada?"}


class FailingEmbeddings:
    """A provider that always refuses, to drive the ingest into `failed`."""

    model = "stub-lexical"
    dim = 768

    async def embed(self, texts: list[str], **_: Any) -> list[list[float]]:
        raise EmbeddingUnavailable("provider refused")

    async def aclose(self) -> None:
        return None


class Harness:
    def __init__(self, **overrides: Any) -> None:
        self.settings = make_db_settings(embed_provider="stub", gcs_bucket="", **overrides)

    async def __aenter__(self) -> Harness:
        truncate_all(self.settings)
        self.database = build_database(self.settings)
        self.objects = InMemoryObjectStore()
        self.embeddings = LexicalStubEmbeddingProvider()
        self.ingestion = self.build(self.embeddings)
        self.documents = DocumentTools(
            sessions=self.database.sessions,
            embeddings=self.embeddings,
            top_k=self.settings.rag_top_k,
            min_score=self.settings.rag_min_score,
            ingestion=self.ingestion,
        )
        return self

    def build(self, embeddings: Any, *, version: int = 1, deadline: float | None = None):
        return DocumentIngestion(
            sessions=self.database.sessions,
            objects=self.objects,
            embeddings=embeddings,
            active_model_name=self.settings.embed_model,
            chunk_chars=self.settings.rag_chunk_chars,
            chunk_overlap=self.settings.rag_chunk_overlap_chars,
            chunk_max=self.settings.rag_chunk_max_chars,
            chunk_min=self.settings.rag_chunk_min_chars,
            pipeline_version=version,
            deadline_seconds=deadline,
        )

    async def __aexit__(self, *_exc: object) -> None:
        await self.database.dispose()

    async def sql(self, query: str, **params: Any) -> list[dict[str, Any]]:
        async with self.database.sessions() as session, session.begin():
            result = await session.execute(sa.text(query).bindparams(**params))
            if not result.returns_rows:
                return []
            return [dict(row) for row in result.mappings().all()]

    async def ingest(self, *, version: int = 1, name: str = "contrato.txt"):
        return await self.build(self.embeddings, version=version).ingest(
            user_id=OWNER, filename=name, data=CONTRACT, declared_mime="text/plain"
        )

    async def search(self, *, min_score: float = 0.0):
        """`min_score=0.0` is the "no floor" baseline: the floor is required now, so an
        unfiltered read is an explicit 0.0 rather than a forgotten `None`."""

        tools = DocumentTools(
            sessions=self.database.sessions,
            embeddings=self.embeddings,
            top_k=self.settings.rag_top_k,
            min_score=min_score,
        )
        result = await tools.search_documents(user_id=OWNER, args=SearchDocumentsArgs(**QUESTION))
        assert result.ok, result.error_code
        return result.data


# --------------------------------------------------------------------------- #
# A1: a failed ingest is retryable, and never reported as success
# --------------------------------------------------------------------------- #
async def test_a_failed_ingest_can_be_retried_and_is_not_reported_as_indexed() -> None:
    """The retry path was designed (the claim predicate accepts `failed`) and unreachable."""

    async with Harness() as h:
        with pytest.raises(EmbeddingUnavailable):
            await h.build(FailingEmbeddings()).ingest(
                user_id=OWNER, filename="contrato.txt", data=CONTRACT, declared_mime="text/plain"
            )

        rows = await h.sql("SELECT ingest_status, chunk_count FROM documents")
        assert rows[0]["ingest_status"] == "failed", "precondition: the ingest must have failed"
        assert rows[0]["chunk_count"] == 0

        retry = await h.ingest()
        assert retry.deduplicated is False, (
            "a failed document was reported as already indexed, so the user is told it succeeded"
        )
        assert retry.status == "ready", f"the retry did not reclaim the row: {retry.status}"
        assert retry.chunk_count > 0

        payload = await h.search()
        assert payload["found"] is True, "the retried document is not retrievable"

        rows = await h.sql("SELECT count(*) AS n FROM documents")
        assert rows[0]["n"] == 1, "the retry created a second document row"


async def test_a_ready_document_is_still_deduplicated() -> None:
    """The early return must survive the fix, but only for `ready`."""

    async with Harness() as h:
        first = await h.ingest()
        assert first.status == "ready" and first.deduplicated is False
        second = await h.ingest()
        assert second.deduplicated is True and second.status == "ready"
        assert second.document_id == first.document_id
        rows = await h.sql("SELECT count(*) AS n FROM documents")
        assert rows[0]["n"] == 1


# --------------------------------------------------------------------------- #
# A10: the version is stamped at finalize, so `ready` always means retrievable
# --------------------------------------------------------------------------- #
async def test_a_document_whose_chunks_are_a_newer_version_stays_retrievable() -> None:
    """The exact failure the reviewer described: chunks at v2, `documents.pipeline_version` at 1.

    `mark_document_ready` now stamps the version in the same statement that flips the row to
    `ready`. Without that, the retrieval join (`documents.pipeline_version = chunks.pipeline_version`)
    matches nothing and the document is `ready` with zero retrievable chunks.
    """

    async with Harness() as h:
        async with h.database.sessions() as session, session.begin():
            document_id = await repositories.create_document(
                session,
                user_id=OWNER,
                original_filename="contrato.txt",
                sanitized_filename="contrato.txt",
                mime_type="text/plain",
                size_bytes=len(CONTRACT),
                content_sha256="0" * 64,
                gcs_uri="",
                pipeline_version=1,
            )
            assert document_id is not None
            await repositories.upsert_embedding_model(
                session,
                model_name=h.settings.embed_model,
                dim=768,
                pipeline_version=2,
                activate=True,
            )

        async with h.database.sessions() as session, session.begin():
            claimed = await repositories.claim_document(
                session, document_id=document_id, executor="test"
            )
            assert claimed is not None, "precondition: the row must be claimable"

        vector = (await h.embeddings.embed(["La fianza depositada fue de dos mensualidades."]))[0]
        async with h.database.sessions() as session, session.begin():
            await repositories.insert_chunks(
                session,
                document_id=document_id,
                owner_user_id=OWNER,
                pipeline_version=2,
                embed_model=h.settings.embed_model,
                chunks=[
                    {
                        "chunk_index": 0,
                        "page_number": 1,
                        "text": "La fianza depositada fue de dos mensualidades.",
                        "char_count": 46,
                        "embedding": vector,
                    }
                ],
            )
        async with h.database.sessions() as session, session.begin():
            stamped = await repositories.mark_document_ready(
                session,
                document_id=document_id,
                chunk_count=1,
                page_count=1,
                pipeline_version=2,
            )
            assert stamped == 1

        rows = await h.sql(
            "SELECT d.pipeline_version AS doc_version, min(c.pipeline_version) AS chunk_version "
            "FROM documents d JOIN document_chunks c ON c.document_id = d.id GROUP BY d.id"
        )
        assert rows[0]["doc_version"] == rows[0]["chunk_version"] == 2

        payload = await h.search()
        assert payload["found"] is True, "a `ready` document must always be retrievable"


async def test_a_new_pipeline_version_produces_its_own_index_and_both_stay_retrievable() -> None:
    """Bumping the version is constructible at all now that it is a parameter."""

    async with Harness() as h:
        first = await h.ingest(version=1)
        second = await h.ingest(version=2)

        assert first.status == "ready" and second.status == "ready"
        rows = await h.sql(
            "SELECT d.pipeline_version AS document, c.pipeline_version AS chunk, count(*) AS n "
            "FROM documents d JOIN document_chunks c ON c.document_id = d.id "
            "GROUP BY d.id, c.pipeline_version ORDER BY document"
        )
        assert {row["document"] for row in rows} == {1, 2}
        assert all(row["document"] == row["chunk"] for row in rows), (
            f"a document row and its chunks disagree about the version: {rows}"
        )
        payload = await h.search()
        assert payload["found"] is True


# --------------------------------------------------------------------------- #
# A2: the relevance floor is applied in the chokepoint
# --------------------------------------------------------------------------- #
async def test_the_relevance_floor_decides_evidence_and_is_applied_in_the_chokepoint() -> None:
    async with Harness() as h:
        await h.ingest()

        unfiltered = await h.search(min_score=0.0)
        assert unfiltered["found"] is True, "precondition: the question must retrieve something"
        assert unfiltered["citations"], "precondition: there must be citations to compare"

        blocked = await h.search(min_score=0.99)
        assert blocked["found"] is False, "a floor of 0.99 must reject everything"
        assert blocked["citations"] == [], "the floor must drop the chunk, not just the flag"
        assert blocked["context"] == ""

        # The floor is a chokepoint argument, so the SQL itself filters and `found` is a real
        # relevance decision rather than `bool(top_k_rows)`.
        async with h.database.sessions() as session:
            active = await repositories.get_active_embedding_model(session)
            vector = (await h.embeddings.embed(["fianza"], task="query"))[0]
            none = await repositories.search_chunks(
                session,
                owner_user_id=OWNER,
                query_embedding=vector,
                embed_model=active.model_name,
                top_k=5,
                min_score=0.99,
            )
            some = await repositories.search_chunks(
                session,
                owner_user_id=OWNER,
                query_embedding=vector,
                embed_model=active.model_name,
                top_k=5,
                min_score=0.0,
            )
        assert none == []
        assert some, "precondition: a floor of 0 must return the rows"


# --------------------------------------------------------------------------- #
# A3: deletion works through the real `main.py` wiring
# --------------------------------------------------------------------------- #
async def test_deletion_is_wired_by_the_application_and_works_through_the_registry() -> None:
    """Every deletion test used to hand-wire `tools.ingestion`; production never did."""

    from app.main import create_app

    settings = make_db_settings(embed_provider="stub", gcs_bucket="")
    truncate_all(settings)
    app = create_app(settings=settings)

    async with app.router.lifespan_context(app):
        assert app.state.document_tools is not None
        assert app.state.document_tools.ingestion is not None, (
            "main.py must inject the pipeline; otherwise the registered tool always fails"
        )
        assert app.state.registry is not None

        result = await app.state.ingestion.ingest(
            user_id=OWNER, filename="contrato.txt", data=CONTRACT, declared_mime="text/plain"
        )
        assert result.status == "ready"

        outcome = await app.state.registry.invoke(
            tool_name="request_document_deletion",
            arguments={"document_id": str(result.document_id)},
            thread_id="delete-test",
            run_key="delete-test:1:request_document_deletion:0",
            attempt_no=1,
            user_id=OWNER,
        )

    assert outcome.ok, f"deletion failed through the real wiring: {outcome.error_code}"
    assert outcome.data.get("complete") is True


# --------------------------------------------------------------------------- #
# A5: /readyz reports the real dependencies
# --------------------------------------------------------------------------- #
async def test_readyz_reports_not_ready_without_a_readable_active_model_row() -> None:
    from app.main import create_app

    settings = make_db_settings(embed_provider="stub", gcs_bucket="")
    truncate_all(settings)
    app = create_app(settings=settings)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            ready = await client.get("/readyz")
            assert ready.status_code == 200, (
                "the migrated database seeds an active model row, so readiness must be green"
            )

            with sa.create_engine(settings.database_sync_url.get_secret_value()).connect() as conn:
                conn.execute(sa.text("UPDATE embedding_models SET is_active = false"))
                conn.commit()

            degraded = await client.get("/readyz")

    assert degraded.status_code == 503
    assert degraded.json()["status"] == "not_ready"
    assert degraded.json()["reason"] == "embedding_model"


async def test_readyz_reports_not_ready_when_postgres_is_unreachable() -> None:
    from app.main import create_app

    settings = make_db_settings(
        embed_provider="stub",
        gcs_bucket="",
        database_url="postgresql+psycopg://postgres:postgres@127.0.0.1:9/taa",
        database_sync_url="postgresql+psycopg://postgres:postgres@127.0.0.1:9/taa",
    )
    app = create_app(settings=settings)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            response = await client.get("/readyz")

    assert response.status_code == 503, "Telegram must be told not to deliver while the DB is down"
    assert response.json()["reason"] == "database"
    body = json.dumps(response.json())
    for forbidden in ("postgres", "127.0.0.1", "9/"):
        assert forbidden not in body, "readiness must not echo the DSN"
