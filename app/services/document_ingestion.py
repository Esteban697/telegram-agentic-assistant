"""Document ingestion: the eleven stages of PLAN.md §6.1.

Layer 3. It owns the pipeline; the SQL lives in `repositories`, the bytes in `storage/gcs`, the
vectors behind the `EmbeddingProvider` port.

The three properties the acceptance criteria rest on:

* **idempotency by content and pipeline version** — the unique index on
  `(user_id, content_sha256, pipeline_version)` means a re-upload resolves to the existing
  document, and the three-part chunk key means a retried ingest converges instead of duplicating;
* **`ready` means complete** — stage 10 runs only after every chunk is committed, and the
  superseded version's rows are deleted only then, so retrieval keeps answering from the previous
  index throughout a re-index;
* **deletion is verified, not assumed** — each stage of `delete_document` records its progress
  and the document is only marked deleted once the object is verifiably gone.

Chunking is page-scoped (a chunk never spans a page) so every citation names exactly one page.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.errors import AssistantError
from app.llm.protocols import EmbeddingProvider
from app.storage import repositories
from app.storage.gcs import ObjectStore, object_key

_logger = logging.getLogger(__name__)

PIPELINE_VERSION = 1
ALLOWED_MIME_TYPES = frozenset({"application/pdf", "text/plain", "text/markdown"})
_TEXT_MIME_TYPES = frozenset({"text/plain", "text/markdown"})
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class UnsupportedDocument(AssistantError):
    code = "unsupported_document"
    message_key = "error.unsupported_document"


class EmptyTextLayer(AssistantError):
    code = "empty_text_layer"
    message_key = "error.empty_text_layer"


class IngestDeadlineExceeded(AssistantError):
    """The inline budget ran out: the document stays queued for the background job (spec §6.1)."""

    code = "ingest_deadline_exceeded"
    message_key = "documents.queued"


class DocumentTooLarge(AssistantError):
    code = "document_too_large"
    message_key = "error.document_too_large"


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    page_number: int | None
    text: str


@dataclass(frozen=True, slots=True)
class Chunk:
    chunk_index: int
    page_number: int | None
    text: str

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass
class IngestResult:
    document_id: UUID | None
    status: str
    chunk_count: int = 0
    page_count: int | None = None
    deduplicated: bool = False
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Stage 3: sanitisation
# --------------------------------------------------------------------------- #
def sanitize_filename(name: str, *, limit: int = 120) -> str:
    """Keep a leaf name that is safe to use as an object path component.

    Path separators, control characters and leading dots are removed; the user's original name is
    stored separately for display, so nothing is lost.
    """

    candidate = _SAFE_NAME_RE.sub("_", (name or "").strip())
    # `..` must not survive anywhere: a leading-dot strip alone left `.._etc_passwd` for
    # "../../etc/passwd", which is a traversal-shaped name even inside a bucket key.
    while ".." in candidate:
        candidate = candidate.replace("..", "_")
    candidate = candidate.strip("._")
    if not candidate:
        candidate = "documento"
    return candidate[:limit]


def sniff_mime(data: bytes, declared: str) -> str:
    """Trust the bytes over the declaration: a PDF that claims to be text is not text."""

    if data[:5] == b"%PDF-":
        return "application/pdf"
    if declared in _TEXT_MIME_TYPES:
        # Stage 2 says to sniff the first bytes: a declared text type is not evidence. Binary
        # bytes declared `text/plain` used to be indexed as mojibake.
        if b"\x00" in data[:4096]:
            return "application/octet-stream"
        try:
            data[:4096].decode("utf-8")
        except UnicodeDecodeError:
            return "application/octet-stream"
        return declared
    if declared in ALLOWED_MIME_TYPES:
        return declared
    return "application/octet-stream"


# --------------------------------------------------------------------------- #
# Stage 6: extraction
# --------------------------------------------------------------------------- #
def extract_pages(data: bytes, mime_type: str) -> list[ExtractedPage]:
    """Text per page. Page numbers are kept because citations depend on them."""

    if mime_type == "application/pdf":
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise UnsupportedDocument("the PDF is encrypted")
            pages = [
                ExtractedPage(page_number=index + 1, text=page.extract_text() or "")
                for index, page in enumerate(reader.pages)
            ]
        except UnsupportedDocument:
            raise
        except Exception as exc:  # noqa: BLE001 - a corrupt PDF is a capability problem
            raise UnsupportedDocument(f"could not read the PDF: {type(exc).__name__}") from exc
    elif mime_type in _TEXT_MIME_TYPES:
        # A form feed is the conventional page separator in plain text, so a text fixture can
        # carry pages and therefore page-level citations (§9.3 asks for the correct page).
        decoded = data.decode("utf-8", errors="replace")
        raw_pages = decoded.split("")
        pages = [
            ExtractedPage(page_number=index + 1, text=page)
            for index, page in enumerate(raw_pages)
        ]
    else:
        raise UnsupportedDocument(f"unsupported type: {mime_type}")

    if not any(page.text.strip() for page in pages):
        # A scanned PDF has no text layer. Saying so is required; silently indexing nothing is
        # the failure spec §9.5 forbids.
        raise EmptyTextLayer("the document has no extractable text")
    return pages


# --------------------------------------------------------------------------- #
# Stage 7: chunking
# --------------------------------------------------------------------------- #
def chunk_pages(
    pages: Iterable[ExtractedPage],
    *,
    chunk_chars: int = 1500,
    overlap_chars: int = 200,
    max_chars: int = 4000,
    min_chars: int = 200,
) -> list[Chunk]:
    """Page-scoped chunking: no chunk spans a page boundary, so each cites exactly one page.

    Splits on paragraph, then line, then sentence, then a hard cut. The overlap is applied only
    inside a page, so it can never pull text from a neighbouring page into a citation.
    """

    if overlap_chars >= chunk_chars:
        raise ValueError("overlap must be smaller than the chunk size")
    if chunk_chars > max_chars:
        raise ValueError("chunk size must not exceed the hard maximum")

    chunks: list[Chunk] = []
    index = 0
    for page in pages:
        text = page.text
        if not text.strip():
            continue
        pieces = _split(text, chunk_chars=chunk_chars, overlap=overlap_chars, max_chars=max_chars)
        # A short tail is merged backwards rather than indexed as a fragment.
        if len(pieces) > 1 and len(pieces[-1]) < min_chars:
            pieces[-2] = f"{pieces[-2]} {pieces[-1]}".strip()
            pieces.pop()
        for piece in pieces:
            chunks.append(Chunk(chunk_index=index, page_number=page.page_number, text=piece))
            index += 1
    return chunks


def _split(text: str, *, chunk_chars: int, overlap: int, max_chars: int) -> list[str]:
    pieces: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + chunk_chars, length)
        if end < length:
            boundary = _best_boundary(text, start, end)
            if boundary > start:
                end = boundary
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= length:
            break
        start = max(end - overlap, start + 1)
    # A hard ceiling, whatever the boundaries did.
    return [piece[:max_chars] for piece in pieces]


def _best_boundary(text: str, start: int, end: int) -> int:
    for separator in ("\n\n", "\n", ". ", "; ", "? ", "! "):
        position = text.rfind(separator, start, end)
        if position > start:
            return position + len(separator)
    return end


# --------------------------------------------------------------------------- #
# The pipeline
# --------------------------------------------------------------------------- #
class DocumentIngestion:
    """Stages 1-11, each a small function with the stage name logged."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        objects: ObjectStore,
        embeddings: EmbeddingProvider,
        active_model_name: str,
        max_upload_mb: int = 20,
        max_pages: int = 200,
        max_ingest_seconds: float = 120.0,
        chunk_chars: int = 1500,
        chunk_overlap: int = 200,
        chunk_max: int = 4000,
        chunk_min: int = 200,
        batch_size: int = 16,
        pipeline_version: int = PIPELINE_VERSION,
        deadline_seconds: float | None = None,
    ) -> None:
        self._sessions = sessions
        self._objects = objects
        self._embeddings = embeddings
        self._model = active_model_name
        self._max_bytes = max_upload_mb * 1024 * 1024
        self._max_pages = max_pages
        self._max_seconds = max_ingest_seconds
        self._chunk = {
            "chunk_chars": chunk_chars,
            "overlap_chars": chunk_overlap,
            "max_chars": chunk_max,
            "min_chars": chunk_min,
        }
        self._batch_size = batch_size
        # The version this ingest writes. It is a parameter rather than the module constant so a
        # re-index is constructible at all — and so the chunks and the document row are stamped
        # with the *same* number, which is what the retrieval join compares.
        self._version = pipeline_version
        # Inline work is bounded by the request budget: the webhook must answer inside Telegram's
        # deadline, and `MAX_INGEST_SECONDS` belongs to the Phase 5 job, not to the inline path.
        self._deadline = deadline_seconds

    # ------------------------------------------------------------------ #
    async def ingest(
        self, *, user_id: int, filename: str, data: bytes, declared_mime: str
    ) -> IngestResult:
        started = time.perf_counter()
        result = IngestResult(document_id=None, status="pending")

        # 2. validate
        if len(data) > self._max_bytes:
            raise DocumentTooLarge(f"{len(data)} bytes exceeds the {self._max_bytes}-byte limit")
        mime_type = sniff_mime(data, declared_mime)
        if mime_type not in ALLOWED_MIME_TYPES:
            # The declared type is user-supplied and must not be echoed into an error or a log
            # (errors.py: "errors never hold the offending value itself").
            raise UnsupportedDocument(
                "unsupported document type", context={"declared_length": len(declared_mime)}
            )

        # 3. sanitize
        sanitized = sanitize_filename(filename)

        # 4. dedupe by content and pipeline version
        digest = hashlib.sha256(data).hexdigest()
        async with self._sessions() as session:
            existing = await repositories.find_document_by_hash(
                session, user_id=user_id, content_sha256=digest, pipeline_version=self._version
            )
        if existing is not None and existing.ingest_status == "ready":
            _logger.info(
                "document already indexed",
                extra={"document_id": str(existing.id), "status": "deduplicated"},
            )
            return IngestResult(
                document_id=existing.id,
                status="ready",
                chunk_count=existing.chunk_count,
                page_count=existing.page_count,
                deduplicated=True,
            )

        # 6-9 happen inside the claim, so a crashed ingest is reclaimable rather than stuck.
        async with self._sessions() as session, session.begin():
            pages = extract_pages(data, mime_type)
            if len(pages) > self._max_pages:
                raise DocumentTooLarge(f"{len(pages)} pages exceeds the {self._max_pages} limit")

            if existing is not None:
                # **A row that exists but is not `ready` is reclaimed, never re-inserted.**
                # `create_document` returns `None` on the unique key, and the old code then
                # reported the *existing* row's status back to the caller — so a re-upload of a
                # document whose ingest failed answered "ese documento ya estaba indexado" for a
                # document with zero retrievable chunks, with no way out. The claim predicate
                # already accepts `failed` (and a stale `processing`), so the retry path existed
                # and was unreachable.
                document_id = existing.id
            else:
                document_id = await repositories.create_document(
                    session,
                    user_id=user_id,
                    original_filename=filename[:200],
                    sanitized_filename=sanitized,
                    mime_type=mime_type,
                    size_bytes=len(data),
                    content_sha256=digest,
                    gcs_uri="",  # filled after the upload
                    pipeline_version=self._version,
                )
            if document_id is None:
                # A concurrent ingest of the same bytes won the index. `deduplicated` is only
                # honest when the winner is `ready`; otherwise the caller must not claim success.
                again = await repositories.find_document_by_hash(
                    session,
                    user_id=user_id,
                    content_sha256=digest,
                    pipeline_version=self._version,
                )
                result.document_id = None if again is None else again.id
                result.status = "pending" if again is None else again.ingest_status
                result.deduplicated = again is not None and again.ingest_status == "ready"
                result.notes.append("another ingest of the same bytes is in flight")
                return result

        # 5. store the original (private, our own object name)
        key = object_key(user_id=user_id, document_id=document_id, sanitized_filename=sanitized)
        uri = await self._objects.put(key=key, data=data, content_type=mime_type)
        async with self._sessions() as session, session.begin():
            await session.execute(
                __import__("sqlalchemy").text(
                    "UPDATE documents SET gcs_uri = :uri WHERE id = :id"
                ).bindparams(uri=uri, id=document_id)
            )

        # claim + chunk + embed + persist
        async with self._sessions() as session, session.begin():
            claimed = await repositories.claim_document(
                session, document_id=document_id, executor=f"ingest:{user_id}"
            )
        if claimed is None:
            result.document_id = document_id
            result.status = "processing"
            result.notes.append("another worker owns the ingest")
            return result

        try:
            chunks = chunk_pages(pages, **self._chunk)
            await self._embed_and_store(
                document_id=document_id, user_id=user_id, chunks=chunks, started=started
            )
        except IngestDeadlineExceeded:
            # The row goes back to `pending`, not to `failed`: the work is unfinished, not wrong,
            # and the claim predicate accepts `pending` for the next attempt.
            async with self._sessions() as session, session.begin():
                await repositories.release_document_claim(session, document_id=document_id)
            _logger.warning(
                "inline ingest budget exceeded; the document stays queued",
                extra={"document_id": str(document_id), "status": "queued"},
            )
            raise
        except AssistantError as exc:
            async with self._sessions() as session, session.begin():
                await repositories.mark_document_failed(
                    session, document_id=document_id, error=exc.code
                )
            _logger.warning("ingest failed", extra=exc.as_log_fields())
            raise

        # 10. finalize: ready only now, and only then supersede the previous version
        async with self._sessions() as session, session.begin():
            await repositories.mark_document_ready(
                session,
                document_id=document_id,
                chunk_count=len(chunks),
                page_count=len([page for page in pages if page.text.strip()]),
                pipeline_version=self._version,
            )
            removed = await repositories.delete_superseded_chunks(
                session, document_id=document_id, keep_pipeline_version=self._version
            )

        elapsed = time.perf_counter() - started
        if elapsed > self._max_seconds:  # pragma: no cover - bounded by the caller too
            result.notes.append(f"ingest took {elapsed:.1f}s, above MAX_INGEST_SECONDS")
        _logger.info(
            "document indexed",
            extra={
                "document_id": str(document_id),
                "chunks": len(chunks),
                "pages": len(pages),
                "superseded_removed": removed,
                "duration_ms": round(elapsed * 1000, 1),
                "status": "ready",
            },
        )
        result.document_id = document_id
        result.status = "ready"
        result.chunk_count = len(chunks)
        result.page_count = len([page for page in pages if page.text.strip()])
        return result

    def _budget_left(self, started: float | None) -> float | None:
        """Seconds of inline budget left, or `None` when no deadline is configured."""

        if self._deadline is None or started is None:
            return None
        return self._deadline - (time.perf_counter() - started)

    async def _embed_and_store(
        self,
        *,
        document_id: UUID,
        user_id: int,
        chunks: Sequence[Chunk],
        started: float | None = None,
    ) -> None:
        """Stage 8 + 9: embed in batches, persist in the same transaction as the activation.

        The deadline bounds the **whole** stage, not just the gaps between batches. Checking it
        only between batches bounded nothing in the case that matters: with the shipped defaults
        the inline budget is ``min(INGEST_INLINE_BUDGET_SECONDS, REQUEST_BUDGET_SECONDS * 0.6)``
        = 27 s while a single `EMBED_TIMEOUT_SECONDS` call may take 30 s, and the webhook `await`s
        the handler, so one slow embed could carry the request past `REQUEST_BUDGET_SECONDS` (45)
        and towards Telegram's 60 s deadline. Each call is now given the *remaining* budget as its
        own timeout and is additionally wrapped in `asyncio.timeout`, so a provider that ignores
        the timeout argument is cancelled too.
        """

        for start in range(0, len(chunks), self._batch_size):
            remaining = self._budget_left(started)
            if remaining is not None and remaining <= 0:
                raise IngestDeadlineExceeded(
                    "inline ingest budget exceeded",
                    context={"deadline_seconds": self._deadline, "batches_done": start},
                )
            batch = chunks[start : start + self._batch_size]
            texts = [chunk.text for chunk in batch]
            if remaining is not None:
                try:
                    async with asyncio.timeout(remaining):
                        vectors = await self._embeddings.embed(texts, timeout=remaining)
                except TimeoutError as exc:
                    raise IngestDeadlineExceeded(
                        "the embedding call outlasted the inline ingest budget",
                        context={"remaining_seconds": round(remaining, 3)},
                    ) from exc
            else:
                vectors = await self._embeddings.embed(texts)
            payload = [
                {
                    "chunk_index": chunk.chunk_index,
                    "page_number": chunk.page_number if chunk.page_number else None,
                    "text": chunk.text,
                    "char_count": chunk.char_count,
                    "embedding": vector,
                }
                for chunk, vector in zip(batch, vectors, strict=True)
            ]
            async with self._sessions() as session, session.begin():
                # The model row is activated in the same transaction as the first chunk batch, so
                # the active model is always the one the vectors were written with (§6.4, RN13).
                await repositories.upsert_embedding_model(
                    session,
                    model_name=self._model,
                    dim=len(vectors[0]) if vectors else 768,
                    pipeline_version=self._version,
                    activate=True,
                )
                await repositories.insert_chunks(
                    session,
                    document_id=document_id,
                    owner_user_id=user_id,
                    pipeline_version=self._version,
                    embed_model=self._model,
                    chunks=payload,
                )

    # ------------------------------------------------------------------ #
    # Deletion: staged and verified (spec §9 "Borrado")
    # ------------------------------------------------------------------ #
    async def delete_document(self, *, user_id: int, document_id: UUID) -> dict[str, Any]:
        async with self._sessions() as session:
            document = await repositories.get_document(
                session, user_id=user_id, document_id=document_id
            )
        if document is None:
            return {"found": False, "complete": True, "notes": ["already gone or not yours"]}

        notes: list[str] = []
        async with self._sessions() as session, session.begin():
            await repositories.set_delete_state(
                session, document_id=document_id, state="requested"
            )

        try:
            async with self._sessions() as session, session.begin():
                removed = await repositories.delete_superseded_chunks(
                    session,
                    document_id=document_id,
                    # "keep nothing": every stored version sorts below this one.
                    keep_pipeline_version=document.pipeline_version + 1,
                )
                remaining = await repositories.count_chunks(session, document_id=document_id)
            if remaining:
                raise AssistantError(f"{remaining} chunks survived the delete")
            async with self._sessions() as session, session.begin():
                await repositories.set_delete_state(
                    session, document_id=document_id, state="chunks_deleted"
                )
            notes.append(f"{removed} chunks removed")

            key = object_key(
                user_id=document.user_id,
                document_id=document_id,
                sanitized_filename=document.sanitized_filename,
            )
            gone = await self._objects.delete(key=key)
            if not gone:  # pragma: no cover - the store verifies this itself
                raise AssistantError("the object could not be verified as deleted")
            async with self._sessions() as session, session.begin():
                await repositories.set_delete_state(
                    session, document_id=document_id, state="object_deleted"
                )
            notes.append("object deleted and verified absent")

            async with self._sessions() as session, session.begin():
                await repositories.mark_document_deleted(session, document_id=document_id)
            notes.append("document marked deleted")
        except AssistantError as exc:
            async with self._sessions() as session, session.begin():
                await repositories.set_delete_state(
                    session, document_id=document_id, state="failed", error=exc.code
                )
            _logger.warning("deletion incomplete", extra=exc.as_log_fields())
            return {"found": True, "complete": False, "notes": [*notes, exc.code]}

        _logger.info(
            "document deleted",
            extra={"document_id": str(document_id), "status": "complete"},
        )
        return {"found": True, "complete": True, "notes": notes}


__all__ = [
    "ALLOWED_MIME_TYPES",
    "PIPELINE_VERSION",
    "Chunk",
    "DocumentIngestion",
    "DocumentTooLarge",
    "EmptyTextLayer",
    "ExtractedPage",
    "IngestResult",
    "UnsupportedDocument",
    "chunk_pages",
    "extract_pages",
    "sanitize_filename",
    "sniff_mime",
]
