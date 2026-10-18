"""Phase 5: the third job, the connection-budget gate, and the tier arithmetic of §11.2/§9.2.

    TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:55432/taa uv run pytest -m integration

`app/jobs/process_documents.py` did not exist before this phase, although PLAN.md §11.1 makes it
one of the four commands in the single image and §11.3 schedules it every five minutes: the
`taa-<env>-job-documents` Cloud Run Job would have run a module that was not in the image. These
tests drive it against the real schema.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from app.jobs import process_documents
from app.llm.embeddings import EmbeddingUnavailable, LexicalStubEmbeddingProvider
from app.services.document_ingestion import DocumentIngestion
from app.storage import repositories
from app.storage.gcs import InMemoryObjectStore, object_key
from app.storage.postgres import build_database
from tests.conftest import TEST_DATABASE_URL, make_db_settings, truncate_all

OWNER = 42

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set"),
]

DOCUMENT = (
    "CONTRATO DE ALQUILER\n\nLa renta mensual es de 950 euros.\n\n"
    "La fianza depositada fue de dos mensualidades.\n"
).encode()


class RefusingEmbeddings:
    """Drives an ingest into `failed`, which is the state the job exists to pick up."""

    model = "refusing"
    dim = 768

    async def embed(self, texts: list[str], **_: Any) -> list[list[float]]:
        raise EmbeddingUnavailable("provider refused")

    async def aclose(self) -> None:
        return None


@pytest.fixture
async def job_harness(monkeypatch: pytest.MonkeyPatch):
    """Real schema, in-memory objects, the lexical stub — and an environment the job can read.

    `process_documents.run()` loads configuration the normal way, so the test sets the variables it
    depends on instead of reaching inside it. `EMBED_PROVIDER=stub` keeps the run credential-free.
    """

    for key, value in (
        ("STORE_BACKEND", "postgres"),
        ("DATABASE_URL", TEST_DATABASE_URL),
        ("DATABASE_SYNC_URL", TEST_DATABASE_URL),
        ("EMBED_PROVIDER", "stub"),
        ("GCS_BUCKET", ""),
        ("GOOGLE_CLOUD_PROJECT", "test-project"),
    ):
        monkeypatch.setenv(key, value)

    settings = make_db_settings(embed_provider="stub", gcs_bucket="")
    truncate_all(settings)
    database = build_database(settings)
    objects = InMemoryObjectStore()
    embeddings = LexicalStubEmbeddingProvider()

    def build(embedder: Any) -> DocumentIngestion:
        return DocumentIngestion(
            sessions=database.sessions,
            objects=objects,
            embeddings=embedder,
            active_model_name=settings.embed_model,
            chunk_chars=settings.rag_chunk_chars,
            chunk_overlap=settings.rag_chunk_overlap_chars,
            chunk_max=settings.rag_chunk_max_chars,
            chunk_min=settings.rag_chunk_min_chars,
        )

    class JobHarness:
        def __init__(self) -> None:
            self.database = database
            self.objects = objects
            self.embeddings = embeddings
            self.settings = settings
            self.build = build

        async def sql(self, query: str, **params: Any) -> list[dict[str, Any]]:
            import sqlalchemy as sa

            async with self.database.sessions() as session, session.begin():
                result = await session.execute(sa.text(query).bindparams(**params))
                if not result.returns_rows:
                    return []
                return [dict(row) for row in result.mappings().all()]

    try:
        yield JobHarness()
    finally:
        await database.dispose()


# --------------------------------------------------------------------------- #
# The job finishes what the inline path could not
# --------------------------------------------------------------------------- #
async def test_the_document_job_indexes_a_document_the_inline_path_failed_on(job_harness) -> None:
    """The whole point of the job: `failed` is a state a background sweep can recover from."""

    failing = job_harness.build(RefusingEmbeddings())
    with pytest.raises(EmbeddingUnavailable):
        await failing.ingest(
            user_id=OWNER, filename="contrato.txt", data=DOCUMENT, declared_mime="text/plain"
        )

    rows = await job_harness.sql("SELECT ingest_status, chunk_count FROM documents")
    assert rows[0]["ingest_status"] == "failed", "precondition: the inline ingest must have failed"

    report = await process_documents.run(
        database=job_harness.database,
        objects=job_harness.objects,
        embeddings=job_harness.embeddings,
    )

    assert report["claimed"] == 1, report
    assert report["ready"] == 1 and report["failed"] == 0, report
    rows = await job_harness.sql("SELECT ingest_status, chunk_count FROM documents")
    assert rows[0]["ingest_status"] == "ready"
    assert rows[0]["chunk_count"] > 0

    # And it is idempotent: the next scheduled run has nothing to do.
    again = await process_documents.run(
        database=job_harness.database,
        objects=job_harness.objects,
        embeddings=job_harness.embeddings,
    )
    assert again["claimed"] == 0, again


async def test_the_job_reports_a_missing_original_instead_of_guessing(job_harness) -> None:
    """A row whose stored original is gone cannot be retried into existence, and says so."""

    ingestion = job_harness.build(job_harness.embeddings)
    result = await ingestion.ingest(
        user_id=OWNER, filename="contrato.txt", data=DOCUMENT, declared_mime="text/plain"
    )
    assert result.status == "ready" and result.document_id is not None

    # Remove the object and put the row back into a claimable state, as a lost upload would.
    key = object_key(
        user_id=OWNER, document_id=result.document_id, sanitized_filename="contrato.txt"
    )
    assert await job_harness.objects.delete(key=key)
    await job_harness.sql(
        "UPDATE documents SET ingest_status = 'pending' WHERE id = :id", id=result.document_id
    )

    report = await process_documents.run(
        database=job_harness.database,
        objects=job_harness.objects,
        embeddings=job_harness.embeddings,
    )

    assert report["failed"] == 1, report
    assert report["results"][0]["error_code"] == "original_missing", report
    rows = await job_harness.sql("SELECT ingest_status FROM documents")
    assert rows[0]["ingest_status"] == "failed", "the state must say what happened"


async def test_the_job_does_not_touch_a_deleted_document(job_harness) -> None:
    """A `/forget`ed document must not be re-ingested by a background sweep."""

    ingestion = job_harness.build(job_harness.embeddings)
    result = await ingestion.ingest(
        user_id=OWNER, filename="contrato.txt", data=DOCUMENT, declared_mime="text/plain"
    )
    # **`ingest_status = 'pending'` as well as deleted**, deliberately: on a `ready` row the
    # claimable predicate would exclude it anyway and this test would pass for the wrong reason.
    # A deleted-but-claimable row is exactly what the `deleted_at IS NULL` clause exists for.
    await job_harness.sql(
        "UPDATE documents SET delete_state = 'complete', deleted_at = now(), "
        "ingest_status = 'pending', chunk_count = 0 WHERE id = :id",
        id=result.document_id,
    )

    report = await process_documents.run(
        database=job_harness.database,
        objects=job_harness.objects,
        embeddings=job_harness.embeddings,
    )

    assert report["claimed"] == 0, report
    rows = await job_harness.sql("SELECT ingest_status FROM documents")
    assert rows[0]["ingest_status"] == "pending", "the sweep must not have touched it"


# --------------------------------------------------------------------------- #
# The claimable scan and the claim predicate are one rule, not two
# --------------------------------------------------------------------------- #
async def test_the_job_scan_and_the_claim_predicate_agree(job_harness) -> None:
    """§5.4's predicate is used by both, so a document one sees cannot be invisible to the other."""

    ingestion = job_harness.build(job_harness.embeddings)
    result = await ingestion.ingest(
        user_id=OWNER, filename="contrato.txt", data=DOCUMENT, declared_mime="text/plain"
    )
    assert result.document_id is not None

    async with job_harness.database.sessions() as session:
        ready = await repositories.list_claimable_documents(session, limit=10)
    assert ready == [], "a `ready` document is not claimable"

    await job_harness.sql(
        "UPDATE documents SET ingest_status = 'processing', claimed_at = now() WHERE id = :id",
        id=result.document_id,
    )
    async with job_harness.database.sessions() as session:
        processing = await repositories.list_claimable_documents(session, limit=10)
    assert processing == [], "a live claim must not be stolen by the sweep"

    # Past the lease, the same row becomes claimable — and `claim_document` agrees.
    await job_harness.sql(
        "UPDATE documents SET claimed_at = now() - interval '16 minutes' WHERE id = :id",
        id=result.document_id,
    )
    async with job_harness.database.sessions() as session:
        expired = await repositories.list_claimable_documents(session, limit=10)
        assert [row.id for row in expired] == [result.document_id]

    async with job_harness.database.sessions() as session, session.begin():
        claimed = await repositories.claim_document(
            session, document_id=result.document_id, executor="test"
        )
    assert claimed is not None, "the predicate the scan uses must also let the claim through"


# --------------------------------------------------------------------------- #
# §11.2 / §9.2: the pool budget, tested rather than asserted
# --------------------------------------------------------------------------- #
def test_the_pool_arithmetic_fits_the_configured_tier() -> None:
    """`(DB_POOL_SIZE + CHECKPOINTER_POOL_SIZE) x processes + reserve <= TEST_DB_MAX_CONNECTIONS`.

    `TEST_DB_MAX_CONNECTIONS` is set **explicitly** to the §12 tier's default (50 for the ~1.7 GB
    shared-core tier, 100 for the recommended 1 vCPU / 3.75 GB one) and never read from the local
    container: the local image defaults to 100, so reading it would make this pass whatever the
    real instance allows.
    """

    from scripts.check_pool_budget import required_connections, verdict

    settings = make_db_settings()
    required = required_connections(
        db_pool=settings.db_pool_size,
        checkpointer_pool=settings.checkpointer_pool_size,
        concurrent_processes=6,
    )

    assert required == 42, (
        "7 x 6 = 42 is the number §11.2 derives the tier from; if this changes, the tier changes"
    )

    tier_default = int(os.environ.get("TEST_DB_MAX_CONNECTIONS", "50"))
    ok, message = verdict(required=required, reserve=5, max_connections=tier_default)
    assert ok, message

    # The negative side: the smallest tier really is too small, and the message says what to do.
    ok_small, message_small = verdict(required=required, reserve=5, max_connections=25)
    assert not ok_small
    assert "subir el tier" in message_small and "bajar los pools" in message_small


def test_the_budget_gate_reads_max_connections_from_the_server() -> None:
    """The gate is only a gate if it measures. Measured against the real local PostgreSQL."""

    from scripts.check_pool_budget import measure_max_connections

    measured = measure_max_connections(TEST_DATABASE_URL)

    assert measured >= 50, (
        f"the local server reports max_connections={measured}: the gate would refuse a deploy "
        "that the real tier allows"
    )


# --------------------------------------------------------------------------- #
# B8: the sweep must not starve behind rows that can never succeed
# --------------------------------------------------------------------------- #
async def test_the_job_reaches_a_new_document_sitting_behind_stuck_ones(job_harness) -> None:
    """Reproduced live by the reviewer: ten stuck rows and one new document, and the new one was
    never reached while the scheduler ran the job every five minutes.

    `failed` is claimable for ever, `mark_document_failed` did not bump `attempts`, and the scan
    ordered by `created_at` — so the oldest un-fixable rows held the head of the queue permanently.
    Ordering by `attempts` first (with the bump that makes the count meaningful) puts a
    never-attempted document ahead of a stuck one.
    """

    from uuid import uuid4

    # **The stuck rows are created FIRST**, which is what makes this a starvation test: an
    # age-ordered scan puts them at the head of the queue, and with more of them than one batch
    # holds, the new document is never reached.
    for index in range(5):
        await job_harness.sql(
            "INSERT INTO documents (id, user_id, original_filename, sanitized_filename, mime_type, "
            "size_bytes, gcs_uri, content_sha256, ingest_status, pipeline_version, attempts, "
            "created_at) "
            "VALUES (:id, :user, 'viejo.txt', 'viejo.txt', 'text/plain', 10, '', :sha, "
            "'failed', 1, 5, now() - interval '1 hour')",
            id=uuid4(),
            user=OWNER,
            sha=f"{index:064d}",
        )

    ingestion = job_harness.build(job_harness.embeddings)
    fresh = await ingestion.ingest(
        user_id=OWNER, filename="nuevo.txt", data=DOCUMENT, declared_mime="text/plain"
    )
    assert fresh.status == "ready" and fresh.document_id is not None

    # The new document is "waiting again" — it has been superseded by a newer upload, which is what
    # `pending` means for a row that already has chunks.
    await job_harness.sql(
        "UPDATE documents SET ingest_status = 'pending', chunk_count = 0 WHERE id = :id",
        id=fresh.document_id,
    )

    report = await process_documents.run(
        database=job_harness.database,
        objects=job_harness.objects,
        embeddings=job_harness.embeddings,
        batch_size=3,
    )

    rows = await job_harness.sql(
        "SELECT ingest_status FROM documents WHERE id = :id", id=fresh.document_id
    )
    assert rows[0]["ingest_status"] == "ready", (
        "the new document was starved by the stuck ones: " + str(report)
    )
    assert report["ready"] >= 1, report


async def test_marking_a_document_failed_cannot_clobber_a_ready_one(job_harness) -> None:
    """The guard: a sweep that never claimed must not overwrite a concurrent ingest's `ready`.

    Without `AND ingest_status = 'processing'`, the `original_missing` branch — which used to mark
    a row failed without claiming it first — could flip a document another worker had just
    finished.
    """

    ingestion = job_harness.build(job_harness.embeddings)
    fresh = await ingestion.ingest(
        user_id=OWNER, filename="contrato.txt", data=DOCUMENT, declared_mime="text/plain"
    )
    assert fresh.status == "ready" and fresh.document_id is not None

    async with job_harness.database.sessions() as session, session.begin():
        changed = await repositories.mark_document_failed(
            session, document_id=fresh.document_id, error="original_missing"
        )

    rows = await job_harness.sql(
        "SELECT ingest_status FROM documents WHERE id = :id", id=fresh.document_id
    )
    assert changed is False, "marking failed applied to a row that was not processing"
    assert rows[0]["ingest_status"] == "ready", "a ready document was flipped back to failed"
