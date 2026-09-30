"""Credential encryption for multi-tenant exchange API secrets.

API keys/secrets are encrypted at rest with Fernet (AES-128-CBC + HMAC).
The encryption key lives **outside** the database in the ``QUAD_CREDENTIAL_KEY``
environment variable (base64 urlsafe 32-byte key).

Generate one with::

    venv/Scripts/python -c "from quad.security.secrets import generate_key; print(generate_key())"

Never log plaintext secrets or the key itself.
"""

from __future__ import annotations

import base64
import os

from cryptography.fernet import Fernet, InvalidToken

KEY_ENV_VAR = "QUAD_CREDENTIAL_KEY"


class CredentialKeyError(RuntimeError):
    """Raised when no usable credential encryption key is configured."""


def generate_key() -> str:
    """Generate a fresh Fernet key suitable for ``QUAD_CREDENTIAL_KEY``."""
    return Fernet.generate_key().decode("ascii")


def _load_fernet() -> Fernet:
    raw = os.environ.get(KEY_ENV_VAR, "").strip()
    if not raw:
        raise CredentialKeyError(
            f"{KEY_ENV_VAR} is not set — cannot encrypt/decrypt credentials. "
            f"Generate one with generate_key() and export it."
        )
    try:
        # Validate key shape early (Fernet raises on malformed keys anyway).
        base64.urlsafe_b64decode(raw)
        return Fernet(raw.encode("ascii"))
    except Exception as exc:
        raise CredentialKeyError(f"{KEY_ENV_VAR} is malformed: {exc}") from exc


def encrypt_secret(plaintext: str) -> str:
    """Encrypt an API key/secret; returns the ASCII token for DB storage."""
    if not plaintext:
        raise ValueError("refusing to encrypt an empty secret")
    return _load_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    """Decrypt a token produced by :func:`encrypt_secret`."""
    try:
        return _load_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise CredentialKeyError("credential token failed authentication") from exc
