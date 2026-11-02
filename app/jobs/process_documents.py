"""Document ingestion job (PLAN.md §6.1, §11.3; spec §13 "procesar ingestas pesadas si hace falta").

    python -m app.jobs.process_documents

**Why this job exists.** Inline ingestion is bounded by the request budget (§6.1 stage 8 note): a
document that cannot be finished inside the webhook's deadline is released back to `pending`, and
nothing user-visible retries it. This is the retry — a one-shot job that scans for `pending`,
`failed` or lease-expired documents, re-ingests each from its stored original, and exits.

It is the same pipeline, not a second one: every document goes through
`DocumentIngestion.ingest`, which re-derives the content hash and therefore *reclaims the existing
row* rather than creating a second one. Ownership is decided by `claim_document`'s guarded UPDATE
(the single authority for "reclaimable", `_CLAIMABLE_PREDICATE`), so this job and the webhook can
run at the same time without either taking the other's work. There is no timer and no loop: Cloud
Scheduler starts the process, the process finishes, the process exits (spec §11).

Unlike the inline path it sets **no deadline** — the request budget is the webhook's problem, and a
Cloud Run Job's own timeout is the right bound here.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from app.config import load_settings
from app.errors import AssistantError
from app.jobs import executor_id, report_line
from app.logging_config import configure_logging
from app.storage import repositories
from app.storage.postgres import build_database, use_compatible_event_loop


async def run(
    *,
    batch_size: int | None = None,
    database: Any | None = None,
    objects: Any | None = None,
    embeddings: Any | None = None,
) -> dict[str, Any]:
    """One pass. The three injections exist so the integration suite can drive the real logic
    against a test database, an in-memory object store and the lexical stub, instead of spawning a
    subprocess and hoping its environment matches (§9.2's pattern for every other job).
    """

    settings = load_settings().validate_or_raise()
    configure_logging(
        level=settings.log_level,
        app_env=settings.app_env,
        content_debug=settings.log_content_debug,
    )

    owns_database = database is None
    if owns_database and settings.store_backend != "postgres":
        # There is nothing to scan without the database, and answering "0 documents" would look
        # like a successful empty run instead of a misconfiguration.
        raise RuntimeError(
            "the document job needs STORE_BACKEND=postgres; there is nothing to scan otherwise"
        )

    database = database or build_database(settings)
    limit = settings.document_job_batch_size if batch_size is None else batch_size
    try:
        async with database.sessions() as session:
            documents = await repositories.list_claimable_documents(session, limit=limit)

        if not documents:
            # Nothing claimable: exit before building the GCS and embedding clients.
            # This job is scheduled, so an idle tick should cost one query.
            return {
                "executor": executor_id("documents"),
                "claimed": 0,
                "ready": 0,
                "failed": 0,
                "skipped": 0,
                "results": [],
            }

        # Imported and built only when there is work, so the idle path stays cheap.
        from app.llm.embeddings import build_embedding_provider
        from app.services.document_ingestion import DocumentIngestion
        from app.storage.gcs import build_object_store, object_key

        objects = objects if objects is not None else build_object_store(settings)
        embeddings = embeddings if embeddings is not None else build_embedding_provider(settings)
        ingestion = DocumentIngestion(
            sessions=database.sessions,
            objects=objects,
            embeddings=embeddings,
            active_model_name=settings.embed_model,
            max_upload_mb=settings.max_upload_mb,
            max_pages=settings.max_pdf_pages,
            max_ingest_seconds=settings.max_ingest_seconds,
            chunk_chars=settings.rag_chunk_chars,
            chunk_overlap=settings.rag_chunk_overlap_chars,
            chunk_max=settings.rag_chunk_max_chars,
            chunk_min=settings.rag_chunk_min_chars,
            # `EMBED_BATCH_SIZE` is the pipeline's own default (16); it is not a `Settings` field, so
            # the job deliberately inherits the pipeline's value rather than inventing a second one.
            # No inline deadline: this is the background path (§11.3).
            deadline_seconds=None,
        )

        results: list[dict[str, Any]] = []
        executor = executor_id("documents")
        for document in documents:
            key = object_key(
                user_id=document.user_id,
                document_id=document.id,
                sanitized_filename=document.sanitized_filename,
            )
            data = await objects.get(key=key)
            if data is None:
                # The row exists but the original does not. It is marked `failed` **with a reason**
                # rather than left silently `pending`: the state then says what happened. It stays
                # reclaimable on purpose — a re-upload of the same bytes repairs it, because the
                # pipeline resolves to the same row *and* re-stores the original. What keeps that
                # from starving the queue is `attempts` (bumped by `mark_document_failed`) and the
                # scan's `ORDER BY attempts, created_at`.
                #
                # **Claim before marking.** `mark_document_failed` is guarded on `processing`, so
                # without this the guard would reject it and the row would stay claimable with no
                # record of why; and with an unguarded version, this branch could flip a row a
                # concurrent webhook ingest had just set `ready`.
                async with database.sessions() as session, session.begin():
                    owned = await repositories.claim_document(
                        session, document_id=document.id, executor=executor
                    )
                if owned is None:
                    results.append(
                        {
                            "document_id": str(document.id),
                            "status": "skipped",
                            "error_code": "claim_lost",
                        }
                    )
                    continue
                async with database.sessions() as session, session.begin():
                    await repositories.mark_document_failed(
                        session, document_id=document.id, error="original_missing"
                    )
                results.append(
                    {
                        "document_id": str(document.id),
                        "status": "failed",
                        "error_code": "original_missing",
                    }
                )
                continue
            try:
                outcome = await ingestion.ingest(
                    user_id=document.user_id,
                    filename=document.original_filename,
                    data=data,
                    declared_mime=document.mime_type,
                )
            except AssistantError as exc:
                # `ingest` has already written `failed` + `ingest_error` for this document.
                results.append(
                    {
                        "document_id": str(document.id),
                        "status": "failed",
                        "error_code": exc.code,
                    }
                )
                continue
            results.append(
                {
                    "document_id": str(document.id),
                    "status": outcome.status,
                    "chunks": outcome.chunk_count,
                    "deduplicated": outcome.deduplicated,
                }
            )
    finally:
        if owns_database:
            await database.dispose()

    ready = sum(1 for item in results if item["status"] == "ready")
    failed = sum(1 for item in results if item["status"] == "failed")
    return {
        "executor": executor,
        "claimed": len(documents),
        "ready": ready,
        "failed": failed,
        "skipped": len(results) - ready - failed,
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    # Before asyncio.run creates the loop (Windows: psycopg async needs the selector loop).
    use_compatible_event_loop()
    try:
        payload = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - a job must exit non-zero with a clear reason
        print(report_line({"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]}))
        return 1
    print(report_line({"ok": True, **payload}))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
