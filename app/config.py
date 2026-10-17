"""Typed configuration (PLAN.md §8.1) and the centralised fail-fast list (§7.2).

Layer 0: imports only stdlib, ``pydantic`` and ``pydantic_settings``.

Two rules from the plan drive the design here:

* **Fail-fast and aggregated** — one pass collects *every* problem and raises a single
  :class:`~app.errors.ConfigError` listing them, instead of revealing them one deploy at a
  time.
* **No dead configuration** — every field below is read by a non-test module. Fields that
  belong to later phases (database, embeddings, RAG, reminders) are deliberately absent and
  are added in the phase that consumes them.
"""

from __future__ import annotations

import re
import zoneinfo
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.errors import ConfigError
from app.policy.authz import parse_allow_list
from app.policy.limits import worst_case_provider_seconds

AppEnv = Literal["development", "test", "production"]
StoreBackend = Literal["memory", "postgres"]

# Characters Telegram allows in a webhook secret token (Appendix A.7).
_WEBHOOK_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")

# Slack left inside the request budget after the worst-case provider call, for everything the
# turn still has to do: assemble the prompt, write to the store, call the Bot API with the
# reply, and write the ledger row.
_BUDGET_SAFETY_MARGIN_SECONDS = 2.0


class Settings(BaseSettings):
    """Runtime configuration, read from the environment (and ``.env`` when present)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application ------------------------------------------------------- #
    app_env: AppEnv = "development"
    log_level: str = "INFO"
    log_content_debug: bool = False

    # --- Telegram ---------------------------------------------------------- #
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_webhook_secret: SecretStr = SecretStr("")
    # `NoDecode` is required: for a "complex" field pydantic-settings JSON-decodes the raw
    # environment value *before* any validator runs, so the documented comma-separated form
    # ("1,2") raised a JSONDecodeError and a single id ("42") silently became an int. With
    # decoding disabled the raw string always reaches `_parse_allowed_ids`.
    telegram_allowed_user_ids: Annotated[frozenset[int], NoDecode] = frozenset()
    dev_allow_any_user: bool = False
    telegram_api_base_url: str = "https://api.telegram.org"
    max_message_chars: int = Field(default=4096, gt=0)
    max_webhook_body_bytes: int = Field(default=1_048_576, gt=0)

    # --- LLM --------------------------------------------------------------- #
    ollama_base_url: str = "https://ollama.com"
    ollama_api_key: SecretStr = SecretStr("")
    llm_model: str = "gpt-oss:120b-cloud"
    # Defaults chosen so the provider timeout is *reachable* inside the request budget:
    #   worst case = LLM_TIMEOUT_SECONDS x (LLM_MAX_RETRIES + 1) + backoff + margin
    #              = 18 x 2 + 0.25 + 2.0 = 38.25 s  <=  REQUEST_BUDGET_SECONDS (45 s)
    # With the earlier defaults (30 s x 3 = 90 s against a 45 s budget) the provider timeout
    # could never fire, so every slow turn surfaced as a budget exhaustion and LLM_MAX_RETRIES
    # had no observable effect. `problems()` recomputes this from the configured values.
    llm_timeout_seconds: float = Field(default=18.0, gt=0)
    llm_max_retries: int = Field(default=1, ge=0)
    llm_think: str | None = "low"

    # --- Budget and limits ------------------------------------------------- #
    request_budget_seconds: float = Field(default=45.0, gt=0)
    rate_limit_per_minute: int = Field(default=20, gt=0)

    # --- Documents and RAG (Phase 4) ---------------------------------------- #
    # `vertex` everywhere for the application (§13.1, decided 2026-10-17): one provider, one code
    # path, 768 dimensions. `stub` is the lexical double the credential-free suites use, and it is
    # a production startup error.
    embed_provider: Literal["vertex", "stub"] = "vertex"
    embed_model: str = "gemini-embedding-001"
    embed_dim: int = Field(default=768, gt=0)
    embed_timeout_seconds: float = Field(default=30.0, gt=0)
    google_cloud_project: str = ""
    google_cloud_region: str = "europe-west1"
    gcs_bucket: str = ""
    max_upload_mb: int = Field(default=20, gt=0)
    max_pdf_pages: int = Field(default=200, gt=0)
    max_ingest_seconds: float = Field(default=120.0, gt=0)
    rag_chunk_chars: int = Field(default=1500, gt=0)
    rag_chunk_overlap_chars: int = Field(default=200, ge=0)
    rag_chunk_max_chars: int = Field(default=4000, gt=0)
    rag_chunk_min_chars: int = Field(default=200, ge=0)
    rag_top_k: int = Field(default=5, gt=0)
    rag_max_context_chars: int = Field(default=6000, gt=0)
    # The retrieval floor, in cosine similarity, applied in the single chokepoint. It is a
    # **degenerate-match filter, not a relevance threshold**, and the difference is measured, not
    # stylistic:
    #
    # * with the lexical stub it removes exactly the chunks that share no content word with the
    #   query (they score 0.0000, while the weakest genuine match on the fixture corpus scores
    #   0.1931);
    # * on the **deployed** provider it removes nothing. Measured with `gemini-embedding-001` on
    #   the two-page live fixture: the page that answers the question scored 0.7332 and an
    #   unrelated question scored 0.5425 — Vertex compresses "unrelated" and "paraphrase" into one
    #   band, so **no single global threshold separates them**. A floor between those two numbers
    #   (0.55) would also reject legitimate paraphrases: rephrased questions whose answer *is* in
    #   the corpus score 0.25-0.28 with the stub.
    #
    # The no-evidence rule therefore does not live here: it is carried by the prompt (spec §9
    # "Instruir al modelo para distinguir entre evidencia, inferencia e información no
    # encontrada"), which receives the scores and the excerpts and judges. `search_documents`
    # reports `found`, `evidence`, `best_score` and this floor so that judgement has something
    # truthful to work with. The earlier default (0.35) and the note that called it "the
    # relevance floor" both claimed a separation that does not exist on the shipping provider.
    rag_min_score: float = Field(default=0.10, ge=0.0, le=1.0)
    # The *ceiling* for inline ingestion (spec §6.1: inline only if the budget allows). It is a
    # ceiling, not a deadline: `main.py` takes the smaller of this and a fraction of
    # REQUEST_BUDGET_SECONDS, so a deliberately tight request budget is never exceeded by an
    # ingest that was configured to run for longer. The rest belongs to the Phase 5 job.
    ingest_inline_budget_seconds: float = Field(default=30.0, gt=0)

    # --- Approvals (Phase 3) ------------------------------------------------ #
    # Spec §12: APPROVAL_TTL_MINUTES. An approval is only valid for this long, and the claim
    # re-checks it, so a stale button cannot execute an action hours later.
    approval_ttl_minutes: int = Field(default=30, gt=0)
    # A `started` tool-ledger row older than this is `unknown` rather than retried (N15).
    stale_invocation_seconds: int = Field(default=900, gt=0)

    # --- Stores (Phase 1: in-memory) --------------------------------------- #
    store_backend: StoreBackend = "memory"
    context_max_messages: int = Field(default=20, gt=0)
    context_ttl_seconds: int = Field(default=3600, gt=0)
    update_dedupe_ttl_seconds: int = Field(default=3600, gt=0)
    update_dedupe_max_entries: int = Field(default=10_000, gt=0)

    # --- Database (Phase 2) ------------------------------------------------ #
    # Async DSN for the application (SQLAlchemy + psycopg 3 async); the sync DSN is what
    # Alembic and the scripts use. Decision 1: migrations stay on the well-trodden sync path.
    database_url: SecretStr = SecretStr("")
    database_sync_url: SecretStr = SecretStr("")
    db_pool_size: int = Field(default=4, gt=0)
    checkpointer_pool_size: int = Field(default=3, gt=0)
    migrations_autorun: bool = False
    default_timezone: str = "Europe/Madrid"

    # --- Reminders (Phase 2: claiming logic; scheduling is Phase 5) --------- #
    reminder_batch_size: int = Field(default=50, gt=0)
    reminder_max_attempts: int = Field(default=5, gt=0)
    # Must exceed the dispatcher's own maximum run time, or Phase 0 reconciliation can mark
    # an in-flight send as `unknown` while the message is still being delivered (RN4).
    dispatcher_lease_seconds: int = Field(default=900, gt=0)
    # `telegram` is the only value allowed in production; `record` appends each send to a file
    # so the concurrency acceptance criterion can be observed from outside the process.
    notifications_transport: Literal["telegram", "record"] = "telegram"
    notify_record_path: str = ""

    # ------------------------------------------------------------------ #
    # Validators
    # ------------------------------------------------------------------ #
    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def _parse_allowed_ids(cls, value: Any) -> frozenset[int]:
        """Accept "1, 2  3", a single id, or a real iterable; reject anything non-numeric.

        A typo must not silently become an empty allow-list, which would either lock
        everybody out or — worse, if the emptiness were ignored — admit everybody.

        Note the shapes this has to survive: ``pydantic-settings`` JSON-parses the raw value
        of a complex field, so ``TELEGRAM_ALLOWED_USER_IDS=42`` arrives here as the **int**
        ``42`` and ``[1,2]`` arrives as a list, while ``1,2`` (not valid JSON) arrives as the
        string ``"1,2"``. All three are documented in ``.env.example`` as comma-separated, and
        all three must work — a live run with a single id is what surfaced the gap.
        """

        if value is None or value == "":
            return frozenset()
        if isinstance(value, bool):  # bool is an int subclass; never a valid id here
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS must contain numeric ids")
        if isinstance(value, int):
            return frozenset({value})
        if isinstance(value, str):
            return parse_allow_list(value)
        if isinstance(value, (list, tuple, set, frozenset)):
            parsed: set[int] = set()
            for item in value:
                if isinstance(item, bool):
                    raise ValueError("TELEGRAM_ALLOWED_USER_IDS must contain numeric ids")
                if isinstance(item, int):
                    parsed.add(item)
                elif isinstance(item, str):
                    parsed |= parse_allow_list(item)
                else:
                    raise ValueError(
                        "TELEGRAM_ALLOWED_USER_IDS must contain numeric ids, "
                        f"got {type(item).__name__}"
                    )
            return frozenset(parsed)
        raise ValueError(
            "TELEGRAM_ALLOWED_USER_IDS must be a comma-separated string or a list of ids, "
            f"got {type(value).__name__}"
        )

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = value.strip().upper()
        if upper not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}")
        return upper

    @field_validator("llm_think")
    @classmethod
    def _validate_think(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().lower()
        if cleaned in {"", "off", "false", "none"}:
            return None
        if cleaned not in {"low", "medium", "high"}:
            raise ValueError("LLM_THINK must be one of: off, low, medium, high")
        return cleaned

    # ------------------------------------------------------------------ #
    # Cross-field, aggregated fail-fast checks
    # ------------------------------------------------------------------ #
    def problems(self) -> list[str]:
        """Return every configuration problem, in one pass. Empty list means valid."""

        found: list[str] = []

        worst_case = worst_case_provider_seconds(
            timeout=self.llm_timeout_seconds, max_retries=self.llm_max_retries
        )
        if worst_case + _BUDGET_SAFETY_MARGIN_SECONDS > self.request_budget_seconds:
            found.append(
                "the provider call can outlast the request budget, so the model timeout could "
                "never fire and every slow turn would surface as a budget exhaustion: "
                f"LLM_TIMEOUT_SECONDS({self.llm_timeout_seconds}) x (LLM_MAX_RETRIES"
                f"({self.llm_max_retries}) + 1) + retry backoff = {worst_case:.2f}s, plus a "
                f"{_BUDGET_SAFETY_MARGIN_SECONDS:.1f}s margin, exceeds "
                f"REQUEST_BUDGET_SECONDS({self.request_budget_seconds}). Lower "
                "LLM_TIMEOUT_SECONDS or LLM_MAX_RETRIES, or raise REQUEST_BUDGET_SECONDS "
                "(which must stay below Telegram's 60s webhook deadline)"
            )

        # The plan's rule is "https outside development": local development may point at a
        # localhost model over http, anything else must be TLS. (The earlier form of this
        # check was inverted — it rejected http in development and allowed it everywhere
        # else — and was caught by a test that drives settings through the environment.)
        if self.app_env != "development" and not self.ollama_base_url.startswith("https://"):
            found.append(
                "OLLAMA_BASE_URL must use https:// outside development "
                f"(got {self.ollama_base_url!r})"
            )

        if not self.telegram_api_base_url.startswith(("https://", "http://")):
            found.append("TELEGRAM_API_BASE_URL must be an absolute http(s) URL")

        if self.default_timezone:
            try:
                zoneinfo.ZoneInfo(self.default_timezone)
            except (zoneinfo.ZoneInfoNotFoundError, ValueError):
                found.append(
                    f"DEFAULT_TIMEZONE {self.default_timezone!r} is not a valid IANA zone "
                    "(the task due dates are stored with it)"
                )

        if self.store_backend == "postgres" and not self.database_url.get_secret_value():
            found.append(
                "STORE_BACKEND=postgres requires DATABASE_URL; without it there is nowhere "
                "to persist conversations, memories or reminders"
            )
        if self.database_url.get_secret_value() and not self.database_sync_url.get_secret_value():
            found.append(
                "DATABASE_URL is set but DATABASE_SYNC_URL is not: Alembic and the scripts "
                "need the synchronous DSN (they do not run inside the event loop)"
            )
        if self.notifications_transport == "record" and not self.notify_record_path:
            found.append("NOTIFICATIONS_TRANSPORT=record requires NOTIFY_RECORD_PATH")

        if self.rag_chunk_overlap_chars >= self.rag_chunk_chars:
            found.append(
                "RAG_CHUNK_OVERLAP_CHARS must be smaller than RAG_CHUNK_CHARS, or the splitter "
                "cannot make progress"
            )
        if self.rag_chunk_chars > self.rag_chunk_max_chars:
            found.append("RAG_CHUNK_CHARS must not exceed RAG_CHUNK_MAX_CHARS")
        if self.rag_chunk_max_chars > self.rag_max_context_chars:
            # Otherwise the largest chunk a retriever can emit cannot fit in the context the
            # model is given, and every answer would be assembled from a truncated passage —
            # quietly, because the block renderer keeps the citation either way.
            found.append(
                "RAG_CHUNK_MAX_CHARS must not exceed RAG_MAX_CONTEXT_CHARS: the largest chunk "
                "could never fit in the context sent to the model, so retrieval would answer "
                "from a truncated passage "
                f"({self.rag_chunk_max_chars} > {self.rag_max_context_chars})"
            )
        if self.embed_provider == "vertex" and not self.google_cloud_project:
            found.append(
                "EMBED_PROVIDER=vertex requires GOOGLE_CLOUD_PROJECT (and Application Default "
                "Credentials: `gcloud auth application-default login`)"
            )

        # --- The fail-fast list of PLAN.md §7.2 (production only) ---------- #
        if self.app_env == "production":
            if self.store_backend == "memory":
                found.append(
                    "STORE_BACKEND=memory is not allowed in production: the in-memory "
                    "stores lose update-id idempotency across instances (PLAN.md §7.2)"
                )
            if self.embed_provider == "stub":
                found.append(
                    "EMBED_PROVIDER=stub is not allowed in production: it is the lexical test "
                    "double, not an embedding model"
                )
            if not self.gcs_bucket:
                found.append(
                    "GCS_BUCKET is required in production: originals must be stored durably"
                )
            if self.notifications_transport == "record":
                found.append(
                    "NOTIFICATIONS_TRANSPORT=record is not allowed in production: it writes "
                    "messages to a file instead of delivering them"
                )
            if self.migrations_autorun:
                found.append(
                    "MIGRATIONS_AUTORUN=true is not allowed in production: a schema change "
                    "must never be a side effect of starting a service (PLAN.md §11.6)"
                )
            if self.dev_allow_any_user:
                found.append(
                    "DEV_ALLOW_ANY_USER=true is not allowed in production: it removes "
                    "the allow-list, which is the primary access control"
                )
            if not self.telegram_allowed_user_ids:
                found.append(
                    "TELEGRAM_ALLOWED_USER_IDS is empty in production; deny-by-default "
                    "means the service must not start"
                )
            if not self.telegram_bot_token.get_secret_value():
                found.append("TELEGRAM_BOT_TOKEN is empty in production")
            if not self.telegram_webhook_secret.get_secret_value():
                found.append("TELEGRAM_WEBHOOK_SECRET is empty in production")
            else:
                secret = self.telegram_webhook_secret.get_secret_value()
                if not _WEBHOOK_SECRET_RE.match(secret):
                    found.append(
                        "TELEGRAM_WEBHOOK_SECRET must be 1-256 characters of "
                        "[A-Za-z0-9_-] to be accepted by Telegram setWebhook"
                    )

        return found

    def validate_or_raise(self) -> Settings:
        """Raise one :class:`ConfigError` listing every problem found."""

        problems = self.problems()
        if problems:
            joined = "\n".join(f"  - {item}" for item in problems)
            raise ConfigError(
                f"{len(problems)} configuration problem(s):\n{joined}",
                context={"problem_count": len(problems)},
            )
        return self

    def access_warnings(self) -> list[str]:
        """Return the loud startup warnings the plan requires outside production (§7.2).

        Pure and log-free on purpose: the strings are produced here and emitted by the
        startup wiring, so the wording is unit-testable without capturing log output.

        Two configurations are legitimate in development and dangerous in silence:

        * an **empty allow-list** denies every user, so the bot looks broken rather than
          closed;
        * ``DEV_ALLOW_ANY_USER=true`` admits **everybody**, including strangers who guess the
          bot's name.

        Both must say so at startup; neither did before the Phase 1 review.
        """

        warnings: list[str] = []
        if self.app_env == "production":
            return warnings

        if self.dev_allow_any_user:
            warnings.append(
                "DEV_ALLOW_ANY_USER=true: ANY Telegram user may talk to this bot, including "
                "users who are not in TELEGRAM_ALLOWED_USER_IDS. Never enable this outside "
                "local testing (PLAN.md §7.2)."
            )
        if not self.telegram_allowed_user_ids and not self.dev_allow_any_user:
            warnings.append(
                "TELEGRAM_ALLOWED_USER_IDS is empty in "
                f"{self.app_env}: every user is DENIED by default, so the bot will answer "
                "nobody. Set your numeric id to be able to talk to it (PLAN.md §7.2)."
            )
        if not self.telegram_bot_token.get_secret_value():
            warnings.append(
                "TELEGRAM_BOT_TOKEN is empty: outbound messages cannot be delivered, so "
                "replies will be logged as failures instead of being sent."
            )
        return warnings

    # ------------------------------------------------------------------ #
    # Convenience accessors (the only place secret values are unwrapped)
    # ------------------------------------------------------------------ #
    @property
    def bot_token(self) -> str:
        return self.telegram_bot_token.get_secret_value()

    @property
    def webhook_secret(self) -> str:
        return self.telegram_webhook_secret.get_secret_value()

    @property
    def api_key(self) -> str:
        return self.ollama_api_key.get_secret_value()

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


def load_settings(**overrides: Any) -> Settings:
    """Build settings from the environment, applying ``overrides`` last.

    Raises :class:`ConfigError` on invalid values so that a bad configuration is a
    startup failure rather than a first-request surprise.
    """

    try:
        settings = Settings(**overrides)
    except Exception as exc:  # pydantic ValidationError, kept out of the public API
        raise ConfigError(f"invalid configuration: {exc}") from exc
    return settings.validate_or_raise()


__all__ = ["AppEnv", "Settings", "StoreBackend", "load_settings"]
