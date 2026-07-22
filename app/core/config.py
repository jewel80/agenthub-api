"""Application settings (12-factor: all config via environment variables).

Reads from environment / .env file via pydantic-settings. A single `settings`
instance is imported across the app.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Core ---
    APP_NAME: str = "AgentHub API"
    ENVIRONMENT: str = "development"
    # Dev default = local SQLite (no external service needed).
    # Prod/Supabase example:
    # postgresql+asyncpg://user:pass@host:port/db
    DATABASE_URL: str = "sqlite+aiosqlite:///./agenthub.db"

    JWT_SECRET: str = "dev-only-change-me-REPLACE-WITH-A-STRONG-SECRET-IN-PROD"
    JWT_ALG: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days

    # Comma-separated list of allowed origins.
    CORS_ORIGINS: str = "http://localhost:3000"

    # --- LLM ---
    LLM_PROVIDER: str = "anthropic"  # anthropic | openai | gemini | mock
    ANTHROPIC_API_KEY: str = ""        # direct Anthropic (x-api-key)
    ANTHROPIC_AUTH_TOKEN: str = ""     # Bearer auth for compatible gateways (e.g. z.ai)
    ANTHROPIC_BASE_URL: str = ""       # optional gateway base URL
    ANTHROPIC_MODEL: str = "claude-haiku-4-5"

    # --- Guardrails ---
    RATE_LIMIT_PER_MIN: int = 20  # 0 disables rate limiting

    # --- Pipeline ---
    AGENTS_CSV_PATH: str = "data/agents_sample.csv"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


settings = Settings()
