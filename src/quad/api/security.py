"""Auth primitives: Telegram Login verification + JWT issuance.

Telegram Login Widget algorithm (stable, per Telegram docs):
  secret = sha256(bot_token)
  data_check_string = "\\n".join(f"{k}={v}" for k, v in sorted(payload minus hash))
  hex(hmac_sha256(secret, data_check_string)) == hash
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

import jwt

JWT_ENV_VAR = "QUAD_API_JWT_SECRET"
JWT_ALGORITHM = "HS256"
JWT_TTL_SECONDS = 24 * 3600
LOGIN_MAX_AGE_SECONDS = 24 * 3600


class AuthError(ValueError):
    """Raised for any credential/login verification failure."""


def verify_telegram_login(payload: dict, bot_token: str) -> dict:
    """Verify a Telegram Login Widget payload; return it on success.

    Raises
    ------
    AuthError
        On missing fields, stale auth_date, or hash mismatch. Uses
        ``hmac.compare_digest`` (constant-time).
    """
    data = dict(payload)
    their_hash = str(data.pop("hash", ""))
    if not their_hash or "id" not in data or "auth_date" not in data:
        raise AuthError("telegram payload missing id/auth_date/hash")
    try:
        auth_date = int(data["auth_date"])
    except (TypeError, ValueError) as exc:
        raise AuthError("telegram auth_date malformed") from exc
    if abs(time.time() - auth_date) > LOGIN_MAX_AGE_SECONDS:
        raise AuthError("telegram login expired; please retry")
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hashlib.sha256(bot_token.encode("utf-8")).digest()
    calc = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, their_hash.lower()):
        raise AuthError("telegram hash mismatch")
    return data


def _jwt_secret() -> str:
    secret = os.environ.get(JWT_ENV_VAR, "").strip()
    if len(secret) < 32:
        raise AuthError(f"{JWT_ENV_VAR} must be set (min 32 chars) to issue API tokens")
    return secret


def issue_token(
    tenant_uuid: str,
    telegram_user_id: int | None,
    token_version: int = 1,
) -> str:
    """Issue a signed JWT access token for a tenant."""
    now = int(time.time())
    return jwt.encode(
        {
            "sub": tenant_uuid,
            "tg": telegram_user_id,
            "iat": now,
            "exp": now + JWT_TTL_SECONDS,
            "ver": token_version,
        },
        _jwt_secret(),
        algorithm=JWT_ALGORITHM,
    )


def parse_token(token: str) -> dict:
    """Validate a JWT and return its claims; raises AuthError."""
    try:
        return jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("invalid token") from exc
