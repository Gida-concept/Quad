"""Pydantic schemas for quad-api v1 (request/response DTOs)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    code: str
    message: str


class Envelope(BaseModel):
    ok: bool = True
    data: Any | None = None
    error: ErrorBody | None = None


# ---------------------------------------------------------------------------
# Auth (Telegram Login Widget -> JWT)
# ---------------------------------------------------------------------------


class TelegramLoginBody(BaseModel):
    """Payload forwarded from the Telegram Login Widget (web) or client."""

    id: int = Field(description="Telegram user id")
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    photo_url: str = ""
    auth_date: int = Field(description="Unix seconds when Telegram signed this")
    hash: str = Field(description="Hex HMAC-SHA256 from Telegram")
    invite_code: str | None = Field(
        default=None, description="Optional invite code for new accounts"
    )


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    tenant_uuid: str
    telegram_user_id: int


# ---------------------------------------------------------------------------
# Exchange
# ---------------------------------------------------------------------------


class ExchangeConnectBody(BaseModel):
    api_key: str = Field(min_length=8, max_length=256)
    api_secret: str = Field(min_length=8, max_length=512)
    testnet: bool = True
    confirm_live: bool = Field(
        default=False,
        description="Required explicit confirmation when testnet=false",
    )


class ExchangeConnectResponse(BaseModel):
    verified: bool
    exchange: str = "bybit"
    testnet: bool
    bybit_uid: str = ""
    permissions: str = ""


class ExchangeStatusResponse(BaseModel):
    connected: bool
    exchange: str = "bybit"
    testnet: bool = True
    bybit_uid: str = ""
    last_verified_at: int | None = None
    worker_state: str | None = None
    worker_pid: int | None = None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class ConfigBody(BaseModel):
    market: Literal["linear"] = "linear"  # v1: linear-only; spot arrives in v2
    capital_pct_per_trade: float = Field(default=2.0, ge=0.1, le=100.0)
    leverage: int = Field(default=5, ge=1, le=125)
    take_profit_pct: float = Field(default=50.0, ge=0.0, le=500.0)
    stop_loss_pct: float = Field(default=30.0, ge=0.0, le=500.0)
    strategy: str = Field(default="trend_following", min_length=1, max_length=64)
    max_positions: int = Field(default=1, ge=1, le=5)
    # NOTE: trade symbols are server-owned (config.yaml underlyings) and
    # intentionally NOT user-settable. GET returns them for display only.
    ai_tier: Literal["cheap", "smart"] = "cheap"  # judge model tier (Phase 5)
    ai_max_calls_per_day: int = Field(default=24, ge=1, le=500)
    strategy_mode: Literal["trend", "scalp"] = "trend"  # one or the other, never both
    scalp_tp_pct: float = Field(default=15.0, ge=1.0, le=100.0)
    scalp_sl_pct: float = Field(default=8.0, ge=1.0, le=100.0)
    scalp_max_calls_per_day: int = Field(default=120, ge=1, le=1000)


class ConfigResponse(BaseModel):
    market: str
    capital_pct_per_trade: float
    leverage: int
    take_profit_pct: float
    stop_loss_pct: float
    strategy: str
    symbols: list[str]
    max_positions: int
    ai_tier: str
    ai_max_calls_per_day: int
    strategy_mode: str
    scalp_tp_pct: float
    scalp_sl_pct: float
    scalp_max_calls_per_day: int
    updated_at: int


# ---------------------------------------------------------------------------
# Telegram pairing
# ---------------------------------------------------------------------------


class PairingCodeResponse(BaseModel):
    code: str
    expires_at: int


# ---------------------------------------------------------------------------
# Trading reads
# ---------------------------------------------------------------------------


class PositionOut(BaseModel):
    symbol: str
    side: str
    quantity: str
    entry_price: str
    current_price: str
    unrealized_pnl: str
    leverage: int
    status: str


class OrderOut(BaseModel):
    client_order_id: str
    symbol: str
    side: str
    type: str
    quantity: str
    price: str
    status: str


class PnlSummary(BaseModel):
    realized_today: str
    open_unrealized: str
    currency: str = "USDT"
