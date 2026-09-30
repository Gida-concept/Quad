"""Per-tenant worker config builder (Phase 4 execution plane).

Resolves a tenant's registry rows (credentials + config) into a full
orchestrator config dict: tenant overrides merged over the global base
config, Bybit keys decrypted in memory, Telegram disabled (workers run
headless — the single control-plane bot serves all chats).
"""

from __future__ import annotations

import copy
from typing import Any

import structlog

from quad.persistence import DatabaseManager
from quad.persistence.repositories import (
    ExchangeCredentialRepository,
    TenantConfigRepository,
    TenantRepository,
)
from quad.security import decrypt_secret

logger = structlog.get_logger(__name__)

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]


class WorkerConfigError(RuntimeError):
    """Raised when a tenant cannot be materialised into a worker config."""


async def build_worker_config(
    registry_db: DatabaseManager,
    tenant_uuid: str,
    base_config: dict[str, Any],
) -> dict[str, Any]:
    """Build the orchestrator config dict for one tenant's worker.

    Raises :class:`WorkerConfigError` when the tenant is unknown/inactive,
    has no Bybit credentials, or secrets cannot be decrypted.
    """
    tenants = TenantRepository(registry_db)
    creds = ExchangeCredentialRepository(registry_db)
    configs = TenantConfigRepository(registry_db)

    tenant = await tenants.get_by_uuid(tenant_uuid)
    if tenant is None:
        raise WorkerConfigError(f"unknown tenant {tenant_uuid}")
    if tenant.status != "active":
        raise WorkerConfigError(f"tenant {tenant_uuid} is {tenant.status}")

    cred = await creds.get_active(tenant_uuid)
    if cred is None:
        raise WorkerConfigError(f"tenant {tenant_uuid} has no Bybit credentials")
    try:
        api_key = decrypt_secret(cred.api_key_enc)
        api_secret = decrypt_secret(cred.api_secret_enc)
    except Exception as exc:
        raise WorkerConfigError(f"cannot decrypt credentials: {exc}") from exc

    cfg_row = await configs.get_or_default(tenant_uuid)
    # Symbols are server-owned (base config underlyings): tenants trade the
    # bot's universe on rotation, never a self-picked list.
    base_trading = base_config.get("trading", {}) or {}
    base_ai = base_config.get("ai", {}) or {}
    symbols = [
        str(s).upper()
        for s in (
            base_trading.get("underlyings") or base_ai.get("pairs") or DEFAULT_SYMBOLS
        )
    ]

    cfg = copy.deepcopy(base_config)
    cfg["_tenant_id"] = tenant_uuid
    cfg["_mode"] = "bybit"

    exchange = cfg.setdefault("exchange", {})
    exchange["name"] = "bybit"
    exchange["api_key"] = api_key
    exchange["api_secret"] = api_secret
    exchange.pop("passphrase", None)
    exchange["testnet"] = bool(cred.testnet)
    # Live workers must actually trade: the base config ships _dry_run=true
    # as a safety default, which (via the adapter's dry-run guard) refuses
    # every live order. Testnet workers keep the base value.
    if not bool(cred.testnet):
        cfg["_dry_run"] = False

    trading = cfg.setdefault("trading", {})
    trading["leverage"] = int(cfg_row.leverage)
    trading["default_strategy"] = cfg_row.strategy
    trading["underlyings"] = symbols

    risk = cfg.setdefault("risk", {})
    risk["max_positions"] = int(cfg_row.max_positions)
    sl = risk.setdefault("per_position_sl", {})
    sl["capital_pct"] = float(cfg_row.stop_loss_pct)
    tp = risk.setdefault("per_position_tp", {})
    tp["capital_pct"] = float(cfg_row.take_profit_pct)

    ai = cfg.setdefault("ai", {})
    ai["pairs"] = symbols
    # Phase 5 cost controls: per-tenant judge tier + daily call budget.
    # Smart tier = paid (faster cycles are set by the supervisor profile).
    ai["tier"] = (cfg_row.ai_tier or "cheap").lower()
    try:
        ai["judge_max_calls_per_day"] = int(cfg_row.ai_max_calls_per_day or 24)
    except (TypeError, ValueError):
        ai["judge_max_calls_per_day"] = 24
    # Strategy mode: trend (hourly AI rotation) or scalp (5-min AI-judged
    # batch loop). Cycle cadence follows the tenant's mode.
    mode = (getattr(cfg_row, "strategy_mode", "trend") or "trend").lower()
    if mode not in ("trend", "scalp"):
        mode = "trend"
    ai["mode"] = mode
    trading = cfg.setdefault("trading", {})
    # Worker cadence follows the tenant's mode (base config default is
    # hourly; scalp needs 5-minute loops). Always authoritative here.
    trading["ai_cycle_interval"] = 300 if mode == "scalp" else 3600
    scalp = ai.setdefault("scalp", {})
    scalp["model"] = scalp.get("model", "qwen/qwen3-32b")
    try:
        scalp["max_calls_per_day"] = int(
            getattr(cfg_row, "scalp_max_calls_per_day", 120) or 120
        )
    except (TypeError, ValueError):
        scalp["max_calls_per_day"] = 120
    try:
        scalp["tp_pct"] = float(getattr(cfg_row, "scalp_tp_pct", 15.0) or 15.0)
        scalp["sl_pct"] = float(getattr(cfg_row, "scalp_sl_pct", 8.0) or 8.0)
    except (TypeError, ValueError):
        scalp["tp_pct"], scalp["sl_pct"] = 15.0, 8.0
    scalp.setdefault("timeframes", ["5m"])
    scalp.setdefault("max_hold_seconds", 3600)
    scalp.setdefault("roll_seconds", 1800)
    scalp.setdefault("max_symbols_per_cycle", 2)
    rotation = ai.setdefault("rotation", {})
    # One trade per cycle, rolled hourly: close the previous trade before
    # opening a new one; never hold a stale position past max_hold_seconds.
    rotation.setdefault("close_open_position_each_cycle", True)
    rotation.setdefault("max_hold_seconds", 3600)

    # Workers are headless: the control-plane bot serves every chat.
    telegram = cfg.setdefault("telegram", {})
    telegram["enabled"] = False

    logger.info(
        "worker_config_built",
        tenant=tenant_uuid,
        strategy=cfg_row.strategy,
        symbols=symbols,
        testnet=bool(cred.testnet),
    )
    return cfg
