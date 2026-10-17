"""Document tools and citation rendering (spec §10, PLAN.md §6.2, §6.5).

Layer 4. `search_documents` is the only caller of the retrieval chokepoint, and it is where
citations are rendered, so the format is fixed in one place (§6.2): the model receives evidence
with its source attached, and it cannot invent a page because the page comes from the row.

The **frozen citation format** is:

    [N] {original_filename} — p. {page}   (or "§ {n}" when the format has no pages)

The label is deliberately language-neutral: this module sits *below* the catalogue layer (§2.2)
and must not import `app.bot.messages`, and "p. 3" reads the same in Spanish as in English.
    {chunk text}

Rendered as plain text (no Telegram markup), one block per retrieved chunk, in score order, with
N starting at 1 for the turn. The block is delimited so a document that *contains* the word
"página" cannot be mistaken for a citation.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.errors import CapabilityUnavailable
from app.policy.risk import RiskLevel
from app.storage import repositories
from app.storage.repositories import ChunkMatch
from app.tools.base import ToolResult
from app.tools.registry import ToolSpec

_logger = logging.getLogger(__name__)

# §6.6(1) freezes this delimiter: an explicitly *untrusted* label around document text. The
# earlier `«DOCUMENTO»` form deviated from the plan and, worse, was forgeable: document text was
# not escaped, so a document containing the marker could close the block early and have its
# remainder read as assistant or user content.
CITATION_MARKER = "<<<DOCUMENTO_NO_CONFIABLE"
CITATION_CLOSE = "<<<FIN_DOCUMENTO_NO_CONFIABLE>>>"


# Any run of `<`. Splitting a run apart is what makes the delimiter un-forgeable; see below.
_ANGLE_RUN_RE = re.compile(r"<+")
# Characters that may not survive into a document *name*: quotes and angle brackets terminate the
# header attribute or the tag itself, and control characters (a newline above all) break out of
# the block's line structure.
_NAME_UNSAFE_RE = re.compile(r"[\x00-\x1f\x7f\"<>]")


def escape_document_text(text: str) -> str:
    """Neutralise the delimiter inside document text.

    The first version was `text.replace("<<<", "< <<")`, and it did not work. `str.replace` scans
    left to right and does not re-examine what it wrote, so a run of four `<` became `< <<<`:
    **the number of `<` was preserved**, a contiguous `<<<` reappeared, and a chunk containing
    `<<<<FIN_DOCUMENTO_NO_CONFIABLE>>>` rendered a *second* closing marker. Everything after the
    forged close then sat outside the block the prompt calls data, which is precisely the escape
    the system prompt tells the model not to fall for (spec §9: document text cannot change the
    agent's policies).

    This version puts a space between every `<` of a run, so **no two `<` are ever adjacent** in
    the output. Both delimiters begin with `<<<`, so neither can be reconstructed from document
    text — which is what the old comment claimed and did not deliver. The escape is visible and
    still readable as ordinary text; it is not reversible, which is the price.
    """

    return _ANGLE_RUN_RE.sub(lambda match: " ".join(match.group(0)), text)


def escape_document_name(name: str, *, limit: int = 120) -> str:
    """Neutralise a document name before it is interpolated into the block header or the label.

    The filename was the second half of the same hole, and it needed no document content at all:
    the name comes from Telegram's `file_name` and was interpolated raw, so a file called
    `contrato.pdf">>>` + newline + text closed the header attribute, closed the tag, and injected
    that text as if it were the assistant's own. Quotes, angle brackets and control characters are
    replaced by spaces and the result is collapsed to one line.
    """

    cleaned = _NAME_UNSAFE_RE.sub(" ", name or "")
    collapsed = " ".join(cleaned.split())
    return collapsed[:limit] or "documento"


def classify_evidence(best_score: float | None, *, floor: float) -> str:
    """The mechanical evidence label: `"none"` or `"matches"`, and nothing in between.

    Deliberately *not* a relevance grade. A cosine score cannot carry one here — see
    `Settings.rag_min_score`, which records that the deployed provider puts an unrelated question
    at 0.5425 and the answering page at 0.7332 — so the code reports the two facts it has (did
    anything clear the floor, and how high was the best) and the prompt does the judging.
    """

    if best_score is None or best_score < floor:
        return "none"
    return "matches"


class SearchDocumentsArgs(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    document_ids: list[UUID] | None = Field(default=None, max_length=20)


class DocumentIdArgs(BaseModel):
    document_id: UUID


@dataclass(frozen=True, slots=True)
class Citation:
    """One retrieved source, in the frozen shape Phase 5 and the report build on.

    `document_name` is **escaped on construction**, not at the render site, because the name is
    interpolated in three places — the header attribute, the label line, and the `citations` list
    in the tool result, which the model reads as JSON. Escaping it here means no construction
    site can forget.
    """

    number: int
    document_id: UUID
    document_name: str
    page_number: int | None
    chunk_index: int
    score: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "document_name", escape_document_name(self.document_name))

    def label(self) -> str:
        where = f"p. {self.page_number}" if self.page_number else f"§ {self.chunk_index + 1}"
        return f"[{self.number}] {self.document_name} — {where}"

    def open_tag(self) -> str:
        where = (
            f'pagina="{self.page_number}"'
            if self.page_number
            else f'bloque="{self.chunk_index + 1}"'
        )
        return f'{CITATION_MARKER} nombre="{self.document_name}" {where}>>>'

    def render(self, text: str) -> str:
        return f"{self.open_tag()}\n{self.label()}\n{escape_document_text(text)}\n{CITATION_CLOSE}"


def render_citations(
    matches: list[ChunkMatch], *, max_chars: int | None = None
) -> tuple[str, list[Citation]]:
    """Return the context block and the citation list, in retrieval order.

    When ``max_chars`` is set, whole blocks are kept until the next one would not fit and the rest
    are dropped — **with their citations**. Slicing the joined string instead cut somewhere inside
    a block, leaving its closing delimiter missing and still reporting a citation for text the
    model never received (§6.6(5) bounds the context; a half-block is not a bound).

    The **first** block is kept even when it alone would not fit, because "the answer is in the
    first thing we retrieved and we threw it away" is worse than a truncated passage — but it is
    truncated *inside* the text, never at the delimiter, so the bound still holds and the reported
    citation still corresponds to what was delivered. Cutting the delimiters instead would
    reintroduce the unterminated-block failure this function exists to prevent. Configuration
    validation keeps `RAG_CHUNK_MAX_CHARS <= RAG_MAX_CONTEXT_CHARS`, so with the real settings the
    first chunk always fits and this is a boundary case.
    """

    citations: list[Citation] = []
    blocks: list[str] = []
    used = 0
    for number, match in enumerate(matches, start=1):
        citation = Citation(
            number=number,
            document_id=match.document_id,
            document_name=match.document_filename,
            page_number=match.page_number,
            chunk_index=match.chunk_index,
            score=match.score,
        )
        block = citation.render(match.text)
        if max_chars is not None and blocks and used + len(block) + 2 > max_chars:
            _logger.info(
                "context truncated at a block boundary",
                extra={"kept": len(blocks), "dropped": len(matches) - len(blocks), "status": "ok"},
            )
            break
        if max_chars is not None and not blocks and len(block) > max_chars:
            escaped = escape_document_text(match.text)
            overhead = len(block) - len(escaped)
            block = citation.render(escaped[: max(0, max_chars - overhead)])
            _logger.info(
                "the first block alone exceeded the context bound and was truncated inside it",
                extra={"kept": 1, "dropped": len(matches) - 1, "status": "truncated"},
            )
        citations.append(citation)
        blocks.append(block)
        used += len(block) + 2
    return "\n\n".join(blocks), citations


class DocumentTools:
    """Retrieval, status and deletion, all owner-scoped by the SQL below them."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        embeddings: Any,
        min_score: float,
        max_context_chars: int = 6000,
        top_k: int = 5,
        ingestion: Any | None = None,
    ) -> None:
        self.sessions = sessions
        self._embeddings = embeddings
        self._max_context = max_context_chars
        self._top_k = top_k
        # The floor from `RAG_MIN_SCORE`. **Required**, not defaulted: it used to default to
        # `None`, and a caller that forgot it silently retrieved unfiltered chunks while the code
        # still read as if the floor were on.
        self._min_score = float(min_score)
        # Deletion is delegated to the pipeline. It used to be attached by the test harnesses
        # (`tools.ingestion = ...`) and never by `main.py`, so the registered tool raised
        # `CapabilityUnavailable` in every real deployment while every test passed.
        self.ingestion = ingestion

    async def search_documents(
        self, *, user_id: int, args: SearchDocumentsArgs, **_: Any
    ) -> ToolResult[dict[str, Any]]:
        """Retrieve, with the owner predicate and the document's version inside the chokepoint.

        **`found` is not a relevance judgement, and the result says so.** It means "at least one
        chunk cleared the degenerate-match floor" — nothing more, because nothing more can be
        established by a cosine threshold here (see `Settings.rag_min_score`: on the deployed
        provider an unrelated question scores 0.5425 and the answering page 0.7332). The payload
        therefore carries

        * `found` — the mechanical fact;
        * `evidence` — `"none"` when the floor dropped everything, `"matches"` otherwise;
        * `best_score` and `floor` — the two numbers the caller (and the prompt) needs to judge
          for itself, presented accurately rather than compressed into a boolean;

        and the no-evidence rule itself is carried by the prompt, as spec §9 states it.
        """

        async with self.sessions() as session:
            active = await repositories.get_active_embedding_model(session)
        if active is None:
            return ToolResult.failure(
                "no_active_embedding_model",
                message_key="error.reindex_required",
                detail="no embedding model is marked active; run ingestion first",
            )

        # `task="query"`: the configured model declares queries and documents differently
        # (asymmetric retrieval), and embedding a query as a document is a silent quality loss.
        vectors = await self._embeddings.embed([args.query], task="query")
        if not vectors:  # pragma: no cover - the provider always returns one vector per input
            return ToolResult.failure("embedding_failed", detail="the provider returned no vector")

        async with self.sessions() as session:
            matches = await repositories.search_chunks(
                session,
                owner_user_id=user_id,
                query_embedding=vectors[0],
                embed_model=active.model_name,
                top_k=self._top_k,
                document_ids=args.document_ids,
                min_score=self._min_score,
            )

        context, citations = render_citations(matches, max_chars=self._max_context)
        best = max((citation.score for citation in citations), default=None)
        _logger.info(
            "documents retrieved",
            extra={
                "chunks": len(matches),
                "status": "ok",
                "embed_model": active.model_name,
                # The score, never the chunk text (§7.4/§6.6(7)): the log carries what the
                # retrieval established, so the no-evidence decision is diagnosable.
                "best_score": None if best is None else round(best, 4),
                "floor": self._min_score,
            },
        )
        return ToolResult.success(
            {
                "found": bool(citations),
                "evidence": classify_evidence(best, floor=self._min_score),
                "best_score": None if best is None else round(best, 4),
                "floor": self._min_score,
                "context": context,
                "citations": [
                    {
                        "number": citation.number,
                        "document_name": citation.document_name,
                        "page_number": citation.page_number,
                        "document_id": str(citation.document_id),
                        "score": round(citation.score, 4),
                    }
                    for citation in citations
                ],
            },
            count=len(matches),
        )

    async def get_document_status(
        self, *, user_id: int, args: DocumentIdArgs, **_: Any
    ) -> ToolResult[dict[str, Any]]:
        async with self.sessions() as session:
            document = await repositories.get_document(
                session, user_id=user_id, document_id=args.document_id
            )
            chunks = (
                0
                if document is None
                else await repositories.count_chunks(session, document_id=document.id)
            )
        if document is None:
            # Another user's id is a not-found, never a confirmation that it exists (§6.5).
            return ToolResult.failure("document_not_found", message_key="error.document_not_found")
        return ToolResult.success(
            {
                "document_name": document.original_filename,
                "ingest_status": document.ingest_status,
                "chunks": chunks,
                "declared_chunks": document.chunk_count,
                "pages": document.page_count,
                "pipeline_version": document.pipeline_version,
                "delete_state": document.delete_state,
            }
        )

    async def request_document_deletion(
        self, *, user_id: int, args: DocumentIdArgs, **_: Any
    ) -> ToolResult[dict[str, Any]]:
        """Records the request; the pipeline does the staged, verified deletion.

        It is level 1 (reversible internal) because it is scoped to one document and confirmed by
        the command that triggers it — spec §6's "/forget ... con confirmación cuando el alcance
        sea amplio".
        """

        ingestion = self.ingestion
        if ingestion is None:
            raise CapabilityUnavailable("deletion is not wired in this configuration")
        result = await ingestion.delete_document(user_id=user_id, document_id=args.document_id)
        if not result.get("complete"):
            return ToolResult.failure(
                "deletion_incomplete",
                message_key="error.deletion_incomplete",
                detail="; ".join(result.get("notes", [])),
            )
        return ToolResult.success(result)


def specs(tools: DocumentTools) -> list[ToolSpec]:
    return [
        ToolSpec(
            name="search_documents",
            description="Search the user's own documents and return the passages with their source.",
            args_model=SearchDocumentsArgs,
            risk_level=RiskLevel.READ,
            timeout_seconds=20.0,
            handler=tools.search_documents,
        ),
        ToolSpec(
            name="get_document_status",
            description="Report a document's ingestion state and chunk count.",
            args_model=DocumentIdArgs,
            risk_level=RiskLevel.READ,
            timeout_seconds=10.0,
            handler=tools.get_document_status,
        ),
        ToolSpec(
            name="request_document_deletion",
            description="Delete one of the user's documents, its chunks and its stored original.",
            args_model=DocumentIdArgs,
            risk_level=RiskLevel.REVERSIBLE,
            timeout_seconds=30.0,
            handler=tools.request_document_deletion,
        ),
    ]


__all__ = [
    "CITATION_CLOSE",
    "CITATION_MARKER",
    "escape_document_name",
    "escape_document_text",
    "Citation",
    "DocumentIdArgs",
    "classify_evidence",
    "DocumentTools",
    "SearchDocumentsArgs",
    "render_citations",
    "specs",
]
