"""Shared Pydantic types.

`Email` validates email SYNTAX via email-validator but skips the DNS/MX
deliverability lookup (which adds latency and rejects valid addresses behind
restrictive networks). Actual deliverability is normally confirmed by an email
verification flow, not at signup time.
"""
from __future__ import annotations

from typing import Annotated

from email_validator import EmailNotValidError, validate_email
from pydantic import BeforeValidator


def _validate_email(value: str) -> str:
    try:
        info = validate_email(str(value), check_deliverability=False)
    except EmailNotValidError as exc:
        raise ValueError(str(exc)) from exc
    return info.normalized


Email = Annotated[str, BeforeValidator(_validate_email)]
