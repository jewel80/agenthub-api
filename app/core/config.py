"""Application settings (12-factor: all config via environment variables).

Reads from environment / .env file via pydantic-settings. A single `settings`
instance is imported across the app. PostgreSQL only — there is no SQLite
fallback: a missing or non-Postgres DATABASE_URL fails at startup.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_JWT_SECRET = "dev-only-change-me-REPLACE-WITH-A-STRONG-SECRET-IN-PROD"

_ALLOWED_ENVIRONMENTS = {"development", "staging", "production"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Core ---
    APP_NAME: str = "AgentHub API"
    ENVIRONMENT: str = "development"  # development | staging | production
    # PostgreSQL (async driver) only. Required — no default, no SQLite.
    # Example: postgresql+asyncpg://<USER>:<PASSWORD>@<HOST>:<PORT>/<DB_NAME>
    DATABASE_URL: str
    # Separate throwaway DB for the test suite (its tables get truncated).
    TEST_DATABASE_URL: str = ""

    # --- Read/write session split (scale §1) ---
    # Empty/disabled => the reader targets DATABASE_URL too (same behavior,
    # just via a read-only session — see app/core/db.py).
    DATABASE_READ_URL: str = ""
    DB_READ_REPLICA_ENABLED: bool = False
    DB_REPLICA_MAX_LAG_SECONDS: int = 2
    READ_YOUR_WRITES_WINDOW_SECONDS: int = 5

    # --- Connection pool / SSL ---
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 5
    DB_POOL_TIMEOUT: int = 10  # seconds waiting for a pooled connection
    DB_SSL_MODE: str = "require"  # require | disable (disable for local dev)
    DB_USE_PGBOUNCER: bool = False  # true when behind a transaction-mode pooler

    # --- Auth ---
    JWT_SECRET: str = _DEV_JWT_SECRET
    JWT_ALG: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days

    # Comma-separated list of allowed origins.
    CORS_ORIGINS: str = "*"
    # CORS_ORIGINS: str = "http://localhost:3000"

    # --- LLM ---
    LLM_PROVIDER: str = "anthropic"  # anthropic | mock
    ANTHROPIC_API_KEY: str = ""        # direct Anthropic (x-api-key)
    ANTHROPIC_AUTH_TOKEN: str = ""     # Bearer auth for compatible gateways (e.g. z.ai)
    ANTHROPIC_BASE_URL: str = ""       # optional gateway base URL
    ANTHROPIC_MODEL: str = "claude-haiku-4-5"
    LLM_TIMEOUT_SECONDS: int = 45
    # Anthropic prompt caching: mark the stable system prompt + conversation
    # prefix with cache_control breakpoints (roadmap §4.1).
    LLM_PROMPT_CACHE_ENABLED: bool = True

    # --- Streaming (roadmap §3) ---
    STREAMING_ENABLED: bool = True
    STREAM_MAX_SECONDS: int = 120
    MAX_CONCURRENT_STREAMS_PER_USER: int = 2  # 0 disables the cap

    # --- Redis (roadmap §5 / scale §2) ---
    # Empty/unreachable Redis => the app degrades to in-memory rate limiting
    # and skips caching; it must never crash or take the API down.
    REDIS_URL: str = ""  # e.g. redis://127.0.0.1:6380/0 (see .env.example)

    # --- Transactional outbox (scale §3) ---
    # Off => no event rows are written and the relay/consumer loops are
    # no-ops (falls back to current behavior: no side-channel events).
    OUTBOX_ENABLED: bool = True
    OUTBOX_RELAY_INTERVAL_SECONDS: float = 1.0
    OUTBOX_BATCH_SIZE: int = 100
    OUTBOX_MAX_ATTEMPTS: int = 5  # after this many failed publishes -> DLQ
    OUTBOX_RETENTION_DAYS: int = 7  # cleanup job deletes published rows older than this

    # --- Multi-layer caching (scale §2) ---
    CACHE_ENABLED: bool = True
    CACHE_L1_ENABLED: bool = True
    CACHE_L1_MAX_ITEMS: int = 5000
    CACHE_DEFAULT_TTL_SECONDS: int = 300
    CACHE_TTL_JITTER_PCT: int = 10  # avoid synchronized expiry across keys
    CACHE_NEGATIVE_TTL_SECONDS: int = 30  # TTL for cached "not found" results

    # --- Limits & quotas (roadmap §5) ---
    RATE_LIMIT_IP_PER_MIN: int = 60     # unauthenticated routes; 0 disables
    LOGIN_RATE_LIMIT_PER_MIN: int = 10  # per email+agent; 0 disables
    LOGIN_MAX_FAILURES: int = 5         # before exponential lockout
    DAILY_TOKEN_QUOTA_DEFAULT: int = 200000  # per user/day; 0 disables
    GLOBAL_LLM_CONCURRENCY: int = 10    # in-flight LLM calls; 0 disables

    # --- Guardrails ---
    RATE_LIMIT_PER_MIN: int = 20  # 0 disables rate limiting

    # --- Internal endpoints ---
    # Protects /meta/* in production: requests must send X-Admin-Token with
    # this value. Empty + production => the endpoints are disabled (404).
    ADMIN_TOKEN: str = ""

    # --- Observability ---
    LOG_FORMAT: str = "text"  # text | json (json for log shippers)

    # --- Pipeline ---
    AGENTS_CSV_PATH: str = "data/agents_sample.csv"

    @model_validator(mode="after")
    def _validate_runtime_config(self) -> Settings:
        if self.ENVIRONMENT not in _ALLOWED_ENVIRONMENTS:
            raise ValueError(
                "ENVIRONMENT must be one of: development | staging | production"
            )
        if not self.DATABASE_URL.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "DATABASE_URL must start with postgresql+asyncpg:// "
                "(SQLite is not supported)"
            )
        for name in ("DATABASE_URL", "TEST_DATABASE_URL", "DATABASE_READ_URL"):
            url = getattr(self, name)
            if not url:
                continue
            parts = urlsplit(url)
            userinfo = (parts.username or "") + (parts.password or "")
            if any(c in userinfo for c in "@:/?#"):
                # Raw separators inside the credentials parse differently
                # across DSN parsers (stdlib splits userinfo at the LAST '@',
                # asyncpg at the first) and surface later as cryptic DNS or
                # auth errors. Fail at startup with the actual fix.
                raise ValueError(
                    f"{name}: credentials contain unencoded characters "
                    "(@:/?#) — percent-encode the username/password "
                    "(e.g. '@' -> '%40')"
                )
        if self.TEST_DATABASE_URL and not self.TEST_DATABASE_URL.startswith(
            "postgresql+asyncpg://"
        ):
            raise ValueError(
                "TEST_DATABASE_URL must start with postgresql+asyncpg://"
            )
        if self.DATABASE_READ_URL and not self.DATABASE_READ_URL.startswith(
            "postgresql+asyncpg://"
        ):
            raise ValueError(
                "DATABASE_READ_URL must start with postgresql+asyncpg://"
            )
        if self.DB_SSL_MODE not in {"require", "disable"}:
            raise ValueError("DB_SSL_MODE must be 'require' or 'disable'")
        if self.ENVIRONMENT != "development":
            if self.JWT_SECRET == _DEV_JWT_SECRET:
                raise ValueError(
                    "JWT_SECRET still has the development default — set a "
                    "strong generated secret "
                    "(python -c \"import secrets; print(secrets.token_urlsafe(48))\")"
                )
            if len(self.JWT_SECRET) < 32:
                raise ValueError(
                    "JWT_SECRET must be at least 32 characters outside development"
                )
            if self.ENVIRONMENT == "production" and self.CORS_ORIGINS.strip() == "*":
                raise ValueError(
                    'CORS_ORIGINS must be an explicit allow-list in production '
                    '( "*" is not allowed )'
                )
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


settings = Settings()
