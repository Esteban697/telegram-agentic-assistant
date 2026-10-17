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
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.errors import ConfigError
from app.policy.authz import parse_allow_list

AppEnv = Literal["development", "test", "production"]
StoreBackend = Literal["memory", "postgres"]

# Characters Telegram allows in a webhook secret token (Appendix A.7).
_WEBHOOK_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


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
    llm_timeout_seconds: float = Field(default=30.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0)
    llm_think: str | None = "low"

    # --- Budget and limits ------------------------------------------------- #
    request_budget_seconds: float = Field(default=45.0, gt=0)
    rate_limit_per_minute: int = Field(default=20, gt=0)

    # --- Stores (Phase 1: in-memory) --------------------------------------- #
    store_backend: StoreBackend = "memory"
    context_max_messages: int = Field(default=20, gt=0)
    context_ttl_seconds: int = Field(default=3600, gt=0)
    update_dedupe_ttl_seconds: int = Field(default=3600, gt=0)
    update_dedupe_max_entries: int = Field(default=10_000, gt=0)

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

        if self.llm_timeout_seconds >= self.request_budget_seconds:
            found.append(
                "LLM_TIMEOUT_SECONDS must be smaller than REQUEST_BUDGET_SECONDS "
                f"({self.llm_timeout_seconds} >= {self.request_budget_seconds}); "
                "otherwise the model timeout can never fire before the request budget"
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

        # --- The fail-fast list of PLAN.md §7.2 (production only) ---------- #
        if self.app_env == "production":
            if self.store_backend == "memory":
                found.append(
                    "STORE_BACKEND=memory is not allowed in production: the in-memory "
                    "stores lose update-id idempotency across instances (PLAN.md §7.2)"
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
