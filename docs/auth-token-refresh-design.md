# Design Note — Refresh Tokens & Token Versioning (fix-doc F17)

Status: **design only** — scheduled for implementation in roadmap §10
(Auth upgrade) before GA. Nothing here is implemented yet.

## Problem

Access tokens are long-lived (7 days, `ACCESS_TOKEN_EXPIRE_MINUTES`). A
leaked token is valid for up to a week, there is no revocation, and there is
no way to log a user out of all devices.

## Design

1. **Short-lived access token** (15 min, `ACCESS_TOKEN_TTL_MIN`). Same JWT
   shape as today (`sub`, `agent_id`) so protected endpoints do not change.
2. **Rotating refresh token**: opaque 256-bit random string, stored
   **hashed** (SHA-256) in a `refresh_tokens` table
   `(id, user_id, token_hash, expires_at, revoked_at, replaced_by_id, created_at)`.
   Web clients receive it in an httpOnly + Secure + SameSite cookie; native
   clients in the JSON body.
3. **Rotation**: every `/auth/refresh` issues a new refresh token and marks
   the old one `replaced_by` the new one. Presenting an already-replaced
   token is treated as theft → revoke the whole chain (user's tokens), per
   OAuth 2.0 BCP.
4. **Token version column** on `users` (`token_version int`). The access JWT
   carries `tv`; a mismatch → 401. Bumping `token_version` revokes every
   outstanding access token instantly (logout-all, account compromise).
5. **Endpoints**: `POST /auth/refresh`, `POST /auth/logout` (revoke current
   refresh token), `POST /auth/logout-all` (bump `token_version`).
6. **Migration** (expand → contract): add nullable columns/tables, backfill
   nothing, then enforce. Existing 7-day tokens keep working until expiry
   during a transition window; new logins get short access + refresh.

## Why hashed at rest

A DB leak must not yield usable refresh tokens. SHA-256 (not argon2) is
enough here: the token is a full-entropy 256-bit random value, so
brute-force is infeasible and fast lookup matters (indexed column).

## Out of scope for this note

Email verification and password reset (also roadmap §10) — they reuse the
same single-use, expiring, hashed-token table pattern.
