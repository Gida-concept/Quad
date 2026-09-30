"""Bybit credential verification (pybit, lazy import).

Proves a user-supplied key/secret pair works before we encrypt and store it.
Never logs the key or secret.
"""

from __future__ import annotations

import asyncio

import structlog

logger = structlog.get_logger(__name__)


class BybitVerifyError(RuntimeError):
    """Raised when Bybit rejects the credentials or is unreachable."""


async def verify_bybit_credentials(
    api_key: str, api_secret: str, *, testnet: bool
) -> dict:
    """Asynchronously verify Bybit API credentials.

    Uses the wallet-balance endpoint (read-only, works on demo + live) and,
    best-effort, the API-key query endpoint for uid/permissions.
    Blocking pybit HTTP calls are offloaded to a thread via asyncio.to_thread.
    """
    try:
        from pybit.unified_trading import HTTP
    except ImportError as exc:
        raise BybitVerifyError("pybit is not installed on the server") from exc

    session = HTTP(
        testnet=testnet, api_key=api_key.strip(), api_secret=api_secret.strip()
    )
    try:
        balance = await asyncio.to_thread(
            session.get_wallet_balance, accountType="UNIFIED"
        )
    except Exception as exc:
        logger.warning("bybit_verify_balance_failed", error=str(exc)[:120])
        raise BybitVerifyError("Bybit rejected the credentials") from exc
    if not isinstance(balance, dict) or balance.get("retCode") != 0:
        msg = (
            (balance or {}).get("retMsg", "unknown")
            if isinstance(balance, dict)
            else "unknown"
        )
        logger.warning("bybit_verify_rejected", ret_msg=str(msg)[:120])
        raise BybitVerifyError("Bybit rejected the credentials")

    meta: dict = {"bybit_uid": "", "permissions": ""}
    try:
        info = await asyncio.to_thread(session.get_api_key_information)
        result = (info or {}).get("result", {}) if isinstance(info, dict) else {}
        meta["bybit_uid"] = str(result.get("userID", ""))
        perms = result.get("permissions", {})
        if isinstance(perms, dict):
            granted = sorted(k for k, v in perms.items() if v)
            meta["permissions"] = ",".join(granted)
        else:
            meta["permissions"] = str(perms)
    except Exception as exc:  # best-effort only; balance already proved the key
        logger.debug("bybit_verify_keyinfo_failed", error=str(exc)[:120])
    return meta
