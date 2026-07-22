"""Security primitives: password hashing (argon2id) + JWT issuance/verification.

Argon2 is wrapped so the hashing backend can be swapped (e.g. to bcrypt)
without touching call sites.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import settings

_hasher = PasswordHasher()


def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        _hasher.verify(hashed, plain)
        return True
    except VerifyMismatchError:
        return False
    except Exception:
        # Malformed hash, unsupported params, etc.
        return False


def create_access_token(
    *, sub: str, agent_id: UUID, extra: dict[str, Any] | None = None
) -> str:
    """Issue a JWT scoped to a specific agent (tenant-scoped auth).

    The `agent_id` claim is the main agent the user authenticated under.
    Protected endpoints validate this claim against the requested resource.
    """
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": sub,
        "agent_id": str(agent_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)).timestamp()),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALG)


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode + verify a JWT. Raises jwt.PyJWTError on any failure."""
    return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALG])
