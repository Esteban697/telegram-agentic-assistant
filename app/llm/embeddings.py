"""Embedding providers (PLAN.md §6.4, §13.1; `PLAN.md` §13.1 decided Vertex on 2026-10-17).

Layer 1. Two implementations behind the `EmbeddingProvider` port declared in Phase 1:

* **`VertexEmbeddingProvider`** — `gemini-embedding-001` on Vertex AI. **This is what the
  application uses** in every environment. 768 dimensions (`outputDimensionality`, which this
  model accepts from 128 to 3072 and recommends at 768), the same as `embeddinggemma`, so the
  `vector(768)` column and every chunk already written stay valid.

  The model was switched from `text-embedding-005` while Phase 4 was being finished: Vertex
  documents that one as "specialized for English and code tasks", and this project's documents
  and product language are Spanish, where an English-optimised embedder degrades retrieval
  quietly. `gemini-embedding-001` is the current unified multilingual model. Because it declares
  the two sides of a search differently, chunk ingestion embeds with `RETRIEVAL_DOCUMENT` and
  retrieval embeds the query with `RETRIEVAL_QUERY`.
* **`LexicalStubEmbeddingProvider`** — a deterministic lexical embedding: a fixed-dimension,
  L2-normalised vector of hashed **content-word** counts (accents folded, Spanish function words
  removed, hash collisions signed), so cosine similarity genuinely tracks term overlap. It exists
  so the credential-free suites can exercise ingestion, storage, retrieval ordering, isolation
  and deletion without a network call, and it is refused in production.

  The first version hashed *every* token, accents and all. Measured on the fixture corpus that
  made the deterministic page evaluation impossible to write honestly: the query "¿Cuál es la
  fecha límite de pago…?" shares the accented `límite` with nothing (the fixtures write `limite`),
  so retrieval ordered the longest page first and answered with the wrong document for four of
  the eight golden questions — while the evaluation that claimed to check ordering passed,
  because it only ever checked the *no-evidence* branch. Folding accents and dropping function
  words is the standard fix and makes the claim in `tests/evals/conftest.py` ("a wrong ordering
  is a real failure, not a coin toss") true rather than aspirational.

**Honest scope.** The stub is not a substitute for a real model: the *scale* of a cosine score
from the stub is meaningless (it is a bag of words), so nothing may calibrate a relevance
threshold against it. What it can carry is *ordering* over a corpus with disjoint vocabulary.
Retrieval quality on realistic documents is measured with Vertex in `tests/live/`.

`format=<JSON Schema>` is not used anywhere: Phase 1's A10 probe measured that it does not
constrain the configured chat model, so a structured decision has to be stated in the prompt.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import unicodedata
from collections.abc import Sequence
from typing import Any

import httpx

from app.errors import AssistantError
from app.llm.protocols import EmbeddingProvider

_logger = logging.getLogger(__name__)

EMBEDDING_DIM = 768
_DEFAULT_TIMEOUT = 30.0
# Asymmetric retrieval: the two sides of a search declare different task types, and embedding a
# query as a document is exactly the kind of quiet quality loss this switch is about.
_TASK_TYPES = {"document": "RETRIEVAL_DOCUMENT", "query": "RETRIEVAL_QUERY"}
# `[0-9a-z]+` is enough because the text is accent-folded *before* tokenising (see `_tokens`).
_TOKEN_RE = re.compile(r"[0-9a-z]+", re.IGNORECASE)

# Spanish function words. Removing them is what turns the stub from "the longest page wins every
# query" into a content-word match: without this, `de`, `la` and `el` dominate the cosine and the
# off-corpus question scores within a hair of a real answer.
#
# A handful of function words that are also *unambiguous Spanish markers* to
# `tests/unit/test_messages.py`'s scanner (`siempre`, `ahora`, `tambien`) are deliberately absent:
# this is data for a tokenizer, not user-facing text, and none of them appears in the fixture
# corpus. Adding one back would be caught by that scanner, which is the right trade.
_STOPWORDS = frozenset(
    """
    a al algo algun alguna algunas alguno algunos ante antes aquel aquella aqui asi aun aunque
    cada como con contra cual cuales cuando cuanto cuantos de del desde donde dos el ella ellas
    ellos en entre era eran es esa esas ese eso esos esta estaba estan estas este esto estos fue
    ha hasta hay la las le les lo los mas me mi mis mucho muy no nos o os otra otras otro otros
    para pero poco por porque que quien quienes se segun ser si sin sobre solo son su sus
    te tiene todo todos tu tus un una uno unos y ya
    """.split()
)


class EmbeddingUnavailable(AssistantError):
    """The provider could not be reached, or is not configured."""

    code = "embedding_unavailable"
    message_key = "error.model_unavailable"


class EmbeddingDimensionMismatch(AssistantError):
    """The provider returned vectors that do not match the configured dimension."""

    code = "embedding_dimension_mismatch"
    message_key = "error.model_unavailable"


# --------------------------------------------------------------------------- #
# Vertex AI
# --------------------------------------------------------------------------- #
class VertexEmbeddingProvider:
    """`gemini-embedding-001` through the Vertex AI REST API.

    Authentication uses Application Default Credentials; there is no key file anywhere. A missing
    ADC file is a *capability* problem, so it surfaces as `EmbeddingUnavailable` with the exact
    remediation rather than as a raw `DefaultCredentialsError`.
    """

    model = "gemini-embedding-001"

    def __init__(
        self,
        *,
        project: str,
        location: str = "europe-west1",
        timeout: float = _DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
        credentials: Any | None = None,
        dim: int = EMBEDDING_DIM,
    ) -> None:
        if not project:
            raise EmbeddingUnavailable(
                "GOOGLE_CLOUD_PROJECT is not set, so Vertex AI cannot be called",
                context={"remediation": "set GOOGLE_CLOUD_PROJECT"},
            )
        self._project = project
        # The configured dimension is what the `vector(768)` column must match (§6.4).
        self.dim = dim
        self._location = location
        self._timeout = timeout
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout))
        self._owns_client = client is None
        self._credentials = credentials

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ #
    async def embed(
        self,
        texts: list[str],
        *,
        timeout: float | None = None,
        task: str = "document",
    ) -> list[list[float]]:
        if not texts:
            return []
        if task not in _TASK_TYPES:
            raise EmbeddingUnavailable(f"unknown embedding task {task!r}")
        token = self._access_token()
        url = (
            f"https://{self._location}-aiplatform.googleapis.com/v1/projects/{self._project}"
            f"/locations/{self._location}/publishers/google/models/{self.model}:predict"
        )
        body = {
            "instances": [{"task_type": _TASK_TYPES[task], "content": text} for text in texts],
            "parameters": {"outputDimensionality": self.dim},
        }
        try:
            response = await self._client.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self._timeout if timeout is None else timeout,
            )
        except httpx.HTTPError as exc:
            raise EmbeddingUnavailable(f"Vertex AI transport failure: {type(exc).__name__}") from exc

        if response.status_code >= 400:
            # The provider's body is not logged or raised: it can echo the input text (§7.4).
            raise EmbeddingUnavailable(
                f"Vertex AI returned {response.status_code} for {len(texts)} input(s)",
                context={"status": response.status_code, "inputs": len(texts)},
            )

        try:
            predictions = response.json()["predictions"]
            vectors = [list(item["embeddings"]["values"]) for item in predictions]
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingUnavailable("Vertex AI returned an unexpected response shape") from exc

        for vector in vectors:
            if len(vector) != self.dim:
                raise EmbeddingDimensionMismatch(
                    f"expected {self.dim} dimensions, got {len(vector)}",
                    context={"configured_dim": self.dim, "returned_dim": len(vector)},
                )
        return vectors

    def _access_token(self) -> str:
        if self._credentials is not None:
            return str(self._credentials.token)

        try:
            import google.auth
            from google.auth.transport.requests import Request as GoogleAuthRequest
        except ImportError as exc:  # pragma: no cover - the dependency is pinned
            raise EmbeddingUnavailable("google-auth is not installed") from exc

        try:
            credentials, _project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            credentials.refresh(GoogleAuthRequest())
        except Exception as exc:  # noqa: BLE001 - any ADC failure is a capability problem
            raise EmbeddingUnavailable(
                "no Application Default Credentials: run "
                "`gcloud auth application-default login` and enable the Vertex AI API",
                context={"remedy": "gcloud auth application-default login"},
            ) from exc
        return str(credentials.token)


# --------------------------------------------------------------------------- #
# The lexical stub
# --------------------------------------------------------------------------- #
def content_words(text: str) -> list[str]:
    """Accent-folded, lower-cased content words: the stub's whole notion of a term.

    Public because the deterministic evaluations assert *why* an ordering is correct — a query
    and the passage that answers it must share one of these.
    """

    folded = unicodedata.normalize("NFD", text.lower())
    plain = "".join(char for char in folded if not unicodedata.combining(char))
    return [
        token
        for token in _TOKEN_RE.findall(plain)
        if len(token) > 1 and token not in _STOPWORDS
    ]


class LexicalStubEmbeddingProvider:
    """A deterministic lexical embedding: signed, hashed content-word counts, L2-normalised.

    Cosine similarity tracks term overlap, so retrieval *ordering* and owner isolation are
    testable without a network. It is not a semantic model, and the scores it produces carry no
    meaning beyond "these two texts share words".
    """

    model = "stub-lexical"
    dim = EMBEDDING_DIM

    async def embed(
        self,
        texts: list[str],
        *,
        timeout: float | None = None,
        task: str = "document",
    ) -> list[list[float]]:
        # The stub is symmetric by construction: it has no task declaration to honour, and it is
        # refused in production. The parameter exists so the port is satisfied honestly.
        del timeout, task
        return [self._vector(text) for text in texts]

    @staticmethod
    def _vector(text: str) -> list[float]:
        vector = [0.0] * EMBEDDING_DIM
        for token in content_words(text):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % EMBEDDING_DIM
            # A second byte breaks the ties that would otherwise bias every token to the same
            # small set of buckets; the third gives the bucket a **sign**, so the rare hash
            # collision between two unrelated terms makes their contribution cancel instead of
            # always adding. Without the sign, four query words against a twelve-word page have
            # a ~6% chance per page of a spurious positive match, which is exactly the margin a
            # relevance floor would be calibrated on.
            vector[index] += (1.0 + digest[4] / 255.0) * (1.0 if digest[5] & 1 else -1.0)
        norm = math.sqrt(sum(component * component for component in vector)) or 1.0
        return [component / norm for component in vector]

    async def aclose(self) -> None:  # pragma: no cover - symmetry with the real provider
        return None


def build_embedding_provider(settings: Any) -> EmbeddingProvider:
    """Choose the provider from configuration.

    `vertex` is the application's provider in every environment (§13.1); `stub` is for the
    credential-free suites and is refused in production by configuration validation.
    """

    if settings.embed_provider == "stub":
        if settings.is_production:  # pragma: no cover - refused by validation
            raise EmbeddingUnavailable("EMBED_PROVIDER=stub is not allowed in production")
        return LexicalStubEmbeddingProvider()
    if settings.embed_provider == "vertex":
        return VertexEmbeddingProvider(
            project=settings.google_cloud_project,
            location=settings.google_cloud_region,
            timeout=settings.embed_timeout_seconds,
            dim=settings.embed_dim,
        )
    raise EmbeddingUnavailable(f"unknown EMBED_PROVIDER: {settings.embed_provider!r}")


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Used by the stub's tests to prove the vectors track term overlap."""

    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(a * a for a in left)) or 1.0
    right_norm = math.sqrt(sum(b * b for b in right)) or 1.0
    return dot / (left_norm * right_norm)


__all__ = [
    "EMBEDDING_DIM",
    "EmbeddingDimensionMismatch",
    "EmbeddingUnavailable",
    "LexicalStubEmbeddingProvider",
    "VertexEmbeddingProvider",
    "build_embedding_provider",
    "content_words",
    "cosine_similarity",
]
