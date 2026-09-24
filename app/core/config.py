"""Application settings (12-factor: all config via environment variables).

Reads from environment / .env file via pydantic-settings. A single `settings`
instance is imported across the app. PostgreSQL only — there is no SQLite
fallback: a missing or non-Postgres DATABASE_URL fails at startup.
"""
from __future__ import annotations

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

    # --- Guardrails ---
    RATE_LIMIT_PER_MIN: int = 20  # 0 disables rate limiting

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
        if self.TEST_DATABASE_URL and not self.TEST_DATABASE_URL.startswith(
            "postgresql+asyncpg://"
        ):
            raise ValueError(
                "TEST_DATABASE_URL must start with postgresql+asyncpg://"
            )
        if self.DB_SSL_MODE not in {"require", "disable"}:
            raise ValueError("DB_SSL_MODE must be 'require' or 'disable'")
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


settings = Settings()
