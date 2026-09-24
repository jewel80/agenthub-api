"""F3 — startup config guard tests.

Outside development the app must refuse to boot with the dev-default (or a
short) JWT_SECRET; production must also refuse wildcard CORS. Tests construct
Settings directly (no .env) so they are hermetic.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings

PG_URL = "postgresql+asyncpg://user:pass@localhost:5432/somedb"
STRONG_SECRET = "x" * 48  # >= 32 chars, not the dev default


def _make(**overrides) -> Settings:
    base = {
        "DATABASE_URL": PG_URL,
        "JWT_SECRET": STRONG_SECRET,
        "CORS_ORIGINS": "https://frontend.example",
        "_env_file": None,  # hermetic: ignore the local .env entirely
    }
    base.update(overrides)
    return Settings(**base)


def test_development_defaults_boot():
    s = _make(
        ENVIRONMENT="development",
        JWT_SECRET="dev-only-change-me-REPLACE-WITH-A-STRONG-SECRET-IN-PROD",
        CORS_ORIGINS="*",
    )
    assert s.ENVIRONMENT == "development"


def test_production_with_dev_default_secret_refuses_to_boot():
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        _make(
            ENVIRONMENT="production",
            JWT_SECRET="dev-only-change-me-REPLACE-WITH-A-STRONG-SECRET-IN-PROD",
        )


def test_production_with_short_secret_refuses_to_boot():
    with pytest.raises(ValidationError, match="32 characters"):
        _make(ENVIRONMENT="production", JWT_SECRET="short-secret")


def test_staging_with_short_secret_refuses_to_boot():
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        _make(ENVIRONMENT="staging", JWT_SECRET="y" * 20)


def test_production_with_strong_secret_boots():
    s = _make(ENVIRONMENT="production")
    assert s.ENVIRONMENT == "production"


def test_production_with_wildcard_cors_refuses_to_boot():
    with pytest.raises(ValidationError, match="CORS_ORIGINS"):
        _make(ENVIRONMENT="production", CORS_ORIGINS="*")


def test_invalid_environment_rejected():
    with pytest.raises(ValidationError, match="ENVIRONMENT"):
        _make(ENVIRONMENT="prod")  # not one of the allowed values


def test_sqlite_database_url_rejected():
    with pytest.raises(ValidationError, match="postgresql\\+asyncpg"):
        _make(DATABASE_URL="sqlite+aiosqlite:///./agenthub.db")


def test_unencoded_at_in_password_rejected_clearly():
    # user:pa@ss@localhost parses differently across DSN parsers; must fail
    # at startup with the encoding fix, not later as a DNS/auth error
    with pytest.raises(ValidationError, match="unencoded characters"):
        _make(DATABASE_URL="postgresql+asyncpg://user:pa@ss@localhost:5432/db")


def test_encoded_password_accepted():
    s = _make(DATABASE_URL="postgresql+asyncpg://user:pa%40ss@localhost:5432/db")
    assert s.DATABASE_URL.startswith("postgresql+asyncpg://user:pa%40ss@")


def test_missing_database_url_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # DATABASE_URL has no default


def test_bad_ssl_mode_rejected():
    with pytest.raises(ValidationError, match="DB_SSL_MODE"):
        _make(DB_SSL_MODE="verify-full")
