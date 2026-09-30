"""Exchange-side flatten for the per-tenant kill-switch (Phase 5).

Cancels every open linear order, then market-closes every open position
(reduce-only).  All calls are best-effort per symbol; failures are collected
as warnings instead of aborting.  Never logs key material.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation

import structlog
from typing import Any, Callable

logger = structlog.get_logger(__name__)


def _default_session_factory(api_key: str, api_secret: str, testnet: bool):
    from pybit.unified_trading import HTTP

    return HTTP(testnet=testnet, api_key=api_key, api_secret=api_secret)


async def flatten_account(
    state: Any,
    tenant_uuid: str,
    session_factory: Callable[..., Any] | None = None,
) -> tuple[list[str], list[str]]:
    """Cancel all open orders and close all positions for a tenant.

    Returns ``(cancelled_order_ids, warnings)``.  Raises only when the
    tenant has no credentials or secrets cannot be decrypted.
    """
    from quad.security import decrypt_secret

    creds = await state.credentials.get_active(tenant_uuid)
    if creds is None:
        return [], ["no exchange credentials connected"]
    try:
        api_key = decrypt_secret(creds.api_key_enc)
        api_secret = decrypt_secret(creds.api_secret_enc)
    except Exception as exc:
        raise RuntimeError(f"cannot decrypt credentials: {exc}") from exc

    factory = session_factory or _default_session_factory
    session = factory(api_key, api_secret, bool(creds.testnet))
    cancelled: list[str] = []
    warnings: list[str] = []

    # 1. Cancel every open linear order in one call.
    try:
        resp = await asyncio.to_thread(
            session.cancel_all_orders, category="linear", settleCoin="USDT"
        )
        for entry in (resp or {}).get("result", {}).get("list", []) or []:
            if entry.get("orderId"):
                cancelled.append(str(entry["orderId"]))
    except Exception as exc:
        warnings.append(f"cancel-all failed: {str(exc)[:160]}")
        logger.warning("flatten_cancel_failed", error=str(exc)[:160])

    # 2. Market-close every open position (reduce-only, opposite side).
    try:
        positions = await asyncio.to_thread(
            session.get_positions, category="linear", settleCoin="USDT"
        )
        entries = (positions or {}).get("result", {}).get("list", []) or []
    except Exception as exc:
        warnings.append(f"position list failed: {str(exc)[:160]}")
        return cancelled, warnings

    for entry in entries:
        try:
            size = str(entry.get("size", "0") or "0")
            try:
                if Decimal(size) <= 0:
                    continue
            except InvalidOperation:
                warnings.append(
                    f"close {entry.get('symbol')} failed: bad size {size!r}"
                )
                continue
            side = entry.get("side", "")
            close_side = "Sell" if side == "Buy" else "Buy"
            params: dict[str, Any] = {
                "category": "linear",
                "symbol": entry.get("symbol"),
                "side": close_side,
                "orderType": "Market",
                "qty": size,
                "reduceOnly": True,
            }
            if entry.get("positionIdx") is not None:
                params["positionIdx"] = int(entry["positionIdx"])
            await asyncio.to_thread(session.place_order, **params)
            cancelled.append(f"closed:{entry.get('symbol')}:{size}")
        except Exception as exc:
            warnings.append(f"close {entry.get('symbol')} failed: {str(exc)[:120]}")
    return cancelled, warnings
