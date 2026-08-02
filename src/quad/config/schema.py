"""Configuration schema validation using Pydantic v2.

Defines expected types, ranges, and constraints for all configuration keys
across trading, exchange, risk, market data, persistence, logging, telegram,
monitoring, and strategy sections.

Usage:
    from quad.config.schema import validate_config

    with open("config/config.yaml") as f:
        raw = yaml.safe_load(f)
    is_valid, errors = validate_config(raw)
"""

from __future__ import annotations

import sys
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# ============================================================================
# Trading Section
# ============================================================================

class TradingConfig(BaseModel):
    """Configuration for trading behavior and parameters."""

    default_strategy: str = Field(
        default="trend_following",
        description="Default strategy name for the bot",
    )
    serial_trade_mode: bool = Field(
        default=True,
        description="Enable serial trade mode (force-close before each ENTER)",
    )
    max_cycle_interval: int = Field(
        default=60,
        ge=1,
        description="Seconds between trading analysis cycles",
    )
    ai_cycle_interval: int = Field(
        default=3600,
        ge=60,
        description="Seconds between AI-driven analysis cycles (default 1 hour)",
    )
    underlyings: list[str] = Field(
        default_factory=lambda: ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "DOGEUSDT"],
        description="List of underlying assets the bot monitors",
    )
    leverage: int = Field(default=1, ge=1, le=125)
    margin_mode: str = Field(default="isolated")
    position_mode: str = Field(default="one_way")

    @field_validator("margin_mode")
    @classmethod
    def validate_margin_mode(cls, v: str) -> str:
        allowed = {"isolated", "cross"}
        if v.lower() not in allowed:
            raise ValueError(f"margin_mode must be one of {allowed}, got '{v}'")
        return v.lower()

    @field_validator("position_mode")
    @classmethod
    def validate_position_mode(cls, v: str) -> str:
        allowed = {"one_way", "hedge"}
        if v.lower() not in allowed:
            raise ValueError(f"position_mode must be one of {allowed}, got '{v}'")
        return v.lower()


# ============================================================================
# Exchange Section
# ============================================================================

class RateLimitConfig(BaseModel):
    """Exchange rate limiting configuration."""

    max_weight: int = Field(
        default=2000,
        ge=100,
        le=2400,
        description="Maximum request weight per window",
    )
    max_orders: int = Field(
        default=900,
        ge=10,
        le=1200,
        description="Maximum orders per second",
    )


class BinanceConfig(BaseModel):
    """Binance-specific adapter configuration.

    Controls URLs, rate-limit warning thresholds, WebSocket reconnection
    backoff, listen-key refresh, request timeouts, and default order params.
    """

    base_url: str = Field(
        default="https://fapi.binance.com",
        description="Binance Futures REST API base URL",
    )
    testnet_base_url: str = Field(
        default="https://testnet.binancefuture.com",
        description="Binance Futures testnet REST API base URL",
    )
    ws_base_url: str = Field(
        default="wss://fstream.binance.com/ws",
        description="Binance Futures WebSocket base URL",
    )
    ws_testnet_base_url: str = Field(
        default="wss://stream.binancefuture.com/ws",
        description="Binance Futures testnet WebSocket base URL",
    )
    header_used_weight: str = Field(
        default="X-MBX-USED-WEIGHT-",
        description="Response header key for used request weight",
    )
    header_order_count: str = Field(
        default="X-MBX-ORDER-COUNT-",
        description="Response header key for order count",
    )
    rate_limit_warn_threshold: float = Field(
        default=0.80, ge=0.0, le=1.0,
        description="Rate-limit warning threshold as fraction of max weight",
    )
    rate_limit_hard_threshold: float = Field(
        default=0.95, ge=0.0, le=1.0,
        description="Rate-limit hard threshold as fraction of max weight",
    )
    ws_backoff_base_seconds: float = Field(
        default=1.0, ge=0.1,
        description="Initial WebSocket reconnection backoff in seconds",
    )
    ws_backoff_max_seconds: float = Field(
        default=30.0, ge=1.0,
        description="Maximum WebSocket reconnection backoff in seconds",
    )
    ws_backoff_multiplier: float = Field(
        default=2.0, ge=1.0,
        description="WebSocket backoff multiplier per retry",
    )
    ws_backoff_jitter_factor: float = Field(
        default=0.1, ge=0.0, le=1.0,
        description="WebSocket backoff jitter factor",
    )
    ws_max_retries: int = Field(
        default=10, ge=1,
        description="Maximum WebSocket reconnection retries",
    )
    listen_key_refresh_seconds: int = Field(
        default=3300, ge=60,
        description="Interval between listen-key refreshes (default 55 min)",
    )
    request_timeout_seconds: float = Field(
        default=30.0, ge=5.0,
        description="REST API request timeout in seconds",
    )
    connect_timeout_seconds: float = Field(
        default=10.0, ge=1.0,
        description="REST API connection timeout in seconds",
    )
    recv_window: int = Field(
        default=5000, ge=1000,
        description="Default receive window for signed requests (ms)",
    )
    heartbeat_seconds: float = Field(
        default=30.0, ge=5.0,
        description="WebSocket heartbeat interval in seconds",
    )
    max_retries: int = Field(
        default=3, ge=1,
        description="Maximum REST API request retries",
    )
    new_order_resp_type: str = Field(
        default="ACK",
        description="newOrderRespType parameter for Binance order placement",
    )
    rate_limiter_wait_seconds: float = Field(
        default=1.0, ge=0.1,
        description="Seconds to wait when rate-limited",
    )
    retry_after_fallback_seconds: float = Field(
        default=5.0, ge=1.0,
        description="Fallback retry-after seconds when header is missing",
    )
    retry_backoff_base: float = Field(
        default=2.0, ge=0.1,
        description="Exponential retry backoff base in seconds for REST API requests",
    )


class OrderGatewayConfig(BaseModel):
    """Order gateway confirmation and retry parameters."""

    confirmation_timeout_seconds: float = Field(
        default=30.0, ge=5.0,
        description="Max seconds to wait for order confirmation",
    )
    max_retries: int = Field(
        default=3, ge=0,
        description="Maximum order placement retries",
    )
    completed_ids_maxlen: int = Field(
        default=1000, ge=100,
        description="Max completed order IDs to track (ring buffer)",
    )
    backoff_base_seconds: float = Field(
        default=2.0, ge=0.1,
        description="Base exponential backoff in seconds for order retries",
    )


class FillReconcilerConfig(BaseModel):
    """Fill reconciler discrepancy tracking parameters."""

    max_discrepancy_history: int = Field(
        default=500, ge=10,
        description="Max discrepancy records to keep in ring buffer",
    )
    stale_order_hours: int = Field(
        default=24, ge=1,
        description="Hours after which an unreconciled order is considered stale",
    )
    recent_discrepancies_default_count: int = Field(
        default=20, ge=1,
        description="Default count for get_recent_discrepancies()",
    )


class ExchangeConfig(BaseModel):
    """Exchange connection and rate limit configuration."""

    name: str = Field(
        default="binance",
        description="Exchange adapter name",
    )
    testnet: bool = Field(
        default=False,
        description="Use testnet environment",
    )
    api_key: str | None = Field(
        default=None,
        description="API key (typically set via env var)",
    )
    api_secret: str | None = Field(
        default=None,
        description="API secret (typically set via env var)",
    )
    rate_limit: RateLimitConfig = Field(
        default_factory=RateLimitConfig,
        description="Rate limiting configuration",
    )
    binance: BinanceConfig = Field(
        default_factory=BinanceConfig,
        description="Binance-specific connection parameters",
    )
    gateway: OrderGatewayConfig = Field(
        default_factory=OrderGatewayConfig,
        description="Order gateway retry and timeout parameters",
    )
    reconciler: FillReconcilerConfig = Field(
        default_factory=FillReconcilerConfig,
        description="Fill reconciler discrepancy tracking parameters",
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """Validate exchange name is supported."""
        allowed = {"binance", "mock"}
        if value.lower() not in allowed:
            raise ValueError(
                f"exchange name must be one of {allowed}, got '{value}'"
            )
        return value.lower()


# ============================================================================
# Risk Section
# ============================================================================

class DailyLossBreakerConfig(BaseModel):
    """Daily loss circuit breaker configuration."""
    pass


class DrawdownBreakerConfig(BaseModel):
    """Drawdown circuit breaker configuration."""
    pass


class ConsecutiveLossesBreakerConfig(BaseModel):
    """Consecutive losses circuit breaker configuration."""

    max_consecutive: int = Field(
        default=5, ge=1,
        description="Max consecutive losing trades before breaker triggers",
    )


class LiquidationCascadeBreakerConfig(BaseModel):
    """Liquidation cascade circuit breaker configuration."""

    min_cascade_distance_pct: float = Field(
        default=0.05, ge=0.0, le=1.0,
        description="Minimum distance to liquidation (decimal) before cascade risk",
    )


class FundingRateSpikeBreakerConfig(BaseModel):
    """Funding rate spike circuit breaker configuration."""

    funding_rate_spike_threshold: float = Field(
        default=0.001, ge=0.0,
        description="Funding rate spike threshold (decimal)",
    )
    max_consecutive_spikes: int = Field(
        default=3, ge=1,
        description="Max consecutive funding rate spikes before escalation",
    )


class VolatilityBreakerConfig(BaseModel):
    """Volatility circuit breaker configuration."""

    volatility_breaker_atr_pct: float = Field(
        default=0.05, ge=0.0,
        description="Volatility breaker ATR threshold (decimal, e.g. 0.05 = 5%)",
    )


class CircuitBreakerConfig(BaseModel):
    """Circuit breaker threshold configuration."""

    # Nested per-breaker configs (used by CircuitBreakerManager at runtime)
    daily_loss: DailyLossBreakerConfig = Field(
        default_factory=DailyLossBreakerConfig,
        description="Daily loss breaker settings",
    )
    drawdown: DrawdownBreakerConfig = Field(
        default_factory=DrawdownBreakerConfig,
        description="Drawdown breaker settings",
    )
    consecutive_losses: ConsecutiveLossesBreakerConfig = Field(
        default_factory=ConsecutiveLossesBreakerConfig,
        description="Consecutive losses breaker settings",
    )
    liquidation_cascade: LiquidationCascadeBreakerConfig = Field(
        default_factory=LiquidationCascadeBreakerConfig,
        description="Liquidation cascade breaker settings",
    )
    funding_rate_spike: FundingRateSpikeBreakerConfig = Field(
        default_factory=FundingRateSpikeBreakerConfig,
        description="Funding rate spike breaker settings",
    )
    volatility: VolatilityBreakerConfig = Field(
        default_factory=VolatilityBreakerConfig,
        description="Volatility breaker settings",
    )

    drawdown_tiers: list[float] = Field(
        default=[5.0, 10.0, 15.0],
        description="Drawdown percentage tiers for escalating responses",
    )

    @field_validator("drawdown_tiers")
    @classmethod
    def validate_drawdown_tiers(cls, value: list[float]) -> list[float]:
        """Ensure drawdown tiers are strictly increasing."""
        if len(value) < 1:
            raise ValueError("At least one drawdown tier is required")
        for i in range(1, len(value)):
            if value[i] <= value[i - 1]:
                raise ValueError(
                    f"Drawdown tiers must be strictly increasing, "
                    f"but tier {i} ({value[i]}) <= tier {i - 1} ({value[i - 1]})"
                )
        return value


class PerPositionSLConfig(BaseModel):
    """Per-position stop-loss configuration."""

    enabled: bool = Field(default=True, description="Enable per-position stop-loss")
    type: Literal["fixed", "trailing"] = Field(default="fixed", description="Stop-loss type")
    capital_pct: float = Field(default=30.0, ge=0.0, le=100.0, description="Stop-loss as percentage of trade capital")


class PerPositionTPConfig(BaseModel):
    """Per-position take-profit configuration."""

    enabled: bool = Field(default=True, description="Enable per-position take-profit")
    type: Literal["fixed"] = Field(default="fixed", description="Take-profit type")
    capital_pct: float = Field(default=50.0, ge=0.0, le=100.0, description="Take-profit as percentage of trade capital")


class KellyConfig(BaseModel):
    """Kelly Criterion sizing parameters."""

    fraction: float = Field(
        default=0.25, ge=0.0, le=1.0,
        description="Fractional Kelly multiplier (fraction of full-Kelly to use)",
    )
    default_fraction: float = Field(
        default=0.02, ge=0.0, le=1.0,
        description="Default position fraction when no trade history is available",
    )


class RiskConfig(BaseModel):
    """Risk management thresholds and limits."""

    max_positions: int = Field(
        default=1, ge=1, le=100,
        description="Maximum number of concurrent positions",
    )
    max_position_size: float = Field(
        default=1000.0,
        ge=1.0,
        description="Maximum position size in USD",
    )
    max_portfolio_risk_pct: float = Field(
        default=20.0,
        ge=0.0,
        le=100.0,
        description="Max percentage of portfolio at risk per trade",
    )
    max_daily_loss_usd: float = Field(
        default=500.0,
        ge=0.0,
        description="Max allowable daily loss in USD",
    )
    max_drawdown_pct: float = Field(
        default=25.0,
        ge=0.0,
        le=100.0,
        description="Max drawdown from peak portfolio value",
    )
    max_leverage: int = Field(
        default=10, ge=1, le=125,
        description="Max leverage enforced by the risk system",
    )
    min_distance_to_liquidation_pct: float = Field(
        default=0.2, ge=0.0, le=1.0,
        description="Minimum distance to liquidation (decimal) before blocking trades",
    )
    funding_rate_periods: int = Field(
        default=3, ge=1,
        description="Number of funding periods (8h each) to use for projected cost checks",
    )
    max_funding_rate_cost: float = Field(
        default=0.001, ge=0.0,
        description="Maximum acceptable funding rate cost per position",
    )
    max_position_concentration: float = Field(
        default=0.4, ge=0.0, le=1.0,
        description="Maximum position concentration as fraction of portfolio",
    )
    min_position_size_usd: float = Field(
        default=10.0, ge=0.0,
        description="Minimum position size in USD",
    )
    max_position_size_pct: float = Field(
        default=0.10, ge=0.0, le=1.0,
        description="Maximum position size as fraction of portfolio",
    )
    max_position_size_usd: float = Field(
        default=10000.0, ge=1.0,
        description="Absolute maximum position size in USD",
    )
    correlation_threshold_pct: float = Field(
        default=60.0, ge=0.0, le=100.0,
        description="Correlation threshold percentage — flags if any quote-asset group exceeds this % of portfolio",
    )
    kelly: KellyConfig = Field(
        default_factory=KellyConfig,
        description="Kelly Criterion sizing parameters",
    )
    circuit_breakers: CircuitBreakerConfig = Field(
        default_factory=CircuitBreakerConfig,
        description="Circuit breaker thresholds",
    )
    per_position_sl: PerPositionSLConfig = Field(
        default_factory=PerPositionSLConfig,
        description="Per-position stop-loss configuration",
    )
    per_position_tp: PerPositionTPConfig = Field(
        default_factory=PerPositionTPConfig,
        description="Per-position take-profit configuration",
    )


# ============================================================================
# Market Data Section
# ============================================================================

class BufferSizesConfig(BaseModel):
    """In-memory buffer sizes for real-time data."""

    ticks: int = Field(
        default=1000,
        ge=100,
        description="Number of price ticks to buffer per symbol",
    )


class CacheTTLConfig(BaseModel):
    """Cache time-to-live values."""

    order_book: int = Field(
        default=5,
        ge=1,
        le=3600,
        description="Order book cache TTL in seconds",
    )
    funding_rate: int = Field(
        default=10, ge=1, le=3600, description="Funding rate cache TTL in seconds"
    )
    mark_price: int = Field(
        default=2, ge=1, le=60, description="Mark price cache TTL in seconds"
    )
    open_interest: int = Field(
        default=3600, ge=60, le=86400,
        description="Open interest cache TTL in seconds",
    )
    order_book_limit: int = Field(
        default=20, ge=5, le=100,
        description="Max order book snapshot depth to cache",
    )


class MarketDataEngineConfig(BaseModel):
    """Market data engine lifecycle parameters."""

    shutdown_timeout_seconds: float = Field(
        default=10.0, ge=1.0,
        description="Max seconds to wait for market data engine shutdown",
    )


class MarketDataBackoffConfig(BaseModel):
    """WebSocket reconnection backoff parameters."""

    base_seconds: float = Field(
        default=1.0, ge=0.1,
        description="Initial WebSocket reconnection backoff in seconds",
    )
    max_seconds: float = Field(
        default=30.0, ge=1.0,
        description="Maximum WebSocket reconnection backoff in seconds",
    )
    multiplier: float = Field(
        default=2.0, ge=1.0,
        description="WebSocket backoff multiplier per retry",
    )
    jitter_fraction: float = Field(
        default=0.1, ge=0.0, le=1.0,
        description="WebSocket backoff jitter fraction",
    )


class MarketDataWebSocketConfig(BaseModel):
    """Market data WebSocket connection parameters."""

    url: str = Field(
        default="wss://fstream.binance.com/ws",
        description="Market data WebSocket base URL",
    )
    backoff: MarketDataBackoffConfig = Field(
        default_factory=MarketDataBackoffConfig,
        description="WebSocket reconnection backoff parameters",
    )
    heartbeat_interval_seconds: float = Field(
        default=30.0, ge=5.0,
        description="WebSocket heartbeat interval in seconds",
    )


class MarketDataConfig(BaseModel):
    """Market data engine configuration."""

    buffer_sizes: BufferSizesConfig = Field(
        default_factory=BufferSizesConfig,
        description="Ring buffer sizes for real-time data",
    )
    cache_ttl: CacheTTLConfig = Field(
        default_factory=CacheTTLConfig,
        description="Cache TTL values in seconds",
    )
    engine: MarketDataEngineConfig = Field(
        default_factory=MarketDataEngineConfig,
        description="Market data engine lifecycle parameters",
    )
    websocket: MarketDataWebSocketConfig = Field(
        default_factory=MarketDataWebSocketConfig,
        description="Market data WebSocket connection parameters",
    )


# ============================================================================
# Persistence Section
# ============================================================================

class DatabasePoolConfig(BaseModel):
    """Database connection pool and retry parameters."""

    min_pool_size: int = Field(
        default=1, ge=1, le=50,
        description="Minimum database pool connections",
    )
    max_pool_size: int = Field(
        default=5, ge=1, le=50,
        description="Maximum database pool connections",
    )
    connect_retry_count: int = Field(
        default=5, ge=1,
        description="Maximum database connection retries",
    )
    command_timeout_seconds: int = Field(
        default=60, ge=5,
        description="Database command timeout in seconds",
    )


class PersistenceConfig(BaseModel):
    """Database persistence configuration."""

    dsn: str = Field(
        default="quad.db",
        description="SQLite database file path (relative or absolute)",
    )
    database: DatabasePoolConfig = Field(
        default_factory=DatabasePoolConfig,
        description="Database connection pool configuration",
    )


# ============================================================================
# Telegram Section
# ============================================================================

class TelegramJobIntervalsConfig(BaseModel):
    """Job interval configuration for the Telegram bot."""

    status_summary_seconds: int = Field(
        default=3600, ge=60,
        description="Interval in seconds for status summary job",
    )
    risk_alert_seconds: int = Field(
        default=300, ge=30,
        description="Interval in seconds for risk alert job",
    )
    funding_rate_countdown_seconds: int = Field(
        default=1800, ge=60,
        description="Interval in seconds for funding rate countdown job",
    )
    liquidation_warning_seconds: int = Field(
        default=300, ge=30,
        description="Interval in seconds for liquidation warning job",
    )

    # First-run delays — how long after bot startup before each job fires
    status_summary_first_seconds: int = Field(
        default=60, ge=0,
        description="Seconds before first status summary job fires",
    )
    risk_alert_first_seconds: int = Field(
        default=120, ge=0,
        description="Seconds before first risk alert job fires",
    )
    funding_rate_countdown_first_seconds: int = Field(
        default=300, ge=0,
        description="Seconds before first funding rate countdown job fires",
    )
    liquidation_warning_first_seconds: int = Field(
        default=180, ge=0,
        description="Seconds before first liquidation warning job fires",
    )


class TelegramDailyReportConfig(BaseModel):
    """Daily report scheduling for the Telegram bot."""

    hour: int = Field(
        default=23, ge=0, le=23,
        description="Hour (UTC) for daily report",
    )
    minute: int = Field(
        default=0, ge=0, le=59,
        description="Minute for daily report",
    )


class TelegramFundingCostReportConfig(BaseModel):
    """Funding cost report scheduling for the Telegram bot."""

    hour: int = Field(
        default=22, ge=0, le=23,
        description="Hour (UTC) for funding cost report",
    )
    minute: int = Field(
        default=0, ge=0, le=59,
        description="Minute for funding cost report",
    )


class TelegramConfig(BaseModel):
    """Telegram bot integration configuration."""

    enabled: bool = Field(default=True, description="Enable Telegram bot")
    job_intervals: TelegramJobIntervalsConfig = Field(
        default_factory=TelegramJobIntervalsConfig,
        description="Bot job interval configuration",
    )
    daily_report: TelegramDailyReportConfig = Field(
        default_factory=TelegramDailyReportConfig,
        description="Daily report scheduling",
    )
    funding_cost_report: TelegramFundingCostReportConfig = Field(
        default_factory=TelegramFundingCostReportConfig,
        description="Funding cost report scheduling",
    )


# ============================================================================
# Monitoring Section
# ============================================================================

class HealthServerConfig(BaseModel):
    """Health check HTTP server configuration."""

    enabled: bool = Field(default=True, description="Enable health server")
    port: int = Field(
        default=9090,
        ge=1024,
        le=65535,
        description="Health server port",
    )
    bind_address: str = Field(
        default="0.0.0.0",
        description="Health server bind address",
    )
    version: str = Field(
        default="0.1.0",
        description="Application version string",
    )


class MetricsConfig(BaseModel):
    """Prometheus metrics configuration."""

    enabled: bool = Field(default=True, description="Enable metrics endpoint")


class MonitoringConfig(BaseModel):
    """System monitoring configuration."""

    health_server: HealthServerConfig = Field(
        default_factory=HealthServerConfig,
        description="Health check server configuration",
    )
    metrics: MetricsConfig = Field(
        default_factory=MetricsConfig,
        description="Metrics configuration",
    )


# ============================================================================
# Strategy Section
# ============================================================================

class TrendFollowingParams(BaseModel):
    """Trend-following strategy parameters using EMA crossover + ADX filter."""

    enabled: bool = False
    fast_ema: int = Field(default=9, ge=1, le=200, description="Fast EMA period")
    slow_ema: int = Field(default=21, ge=1, le=200, description="Slow EMA period")
    adx_period: int = Field(default=14, ge=1, le=50, description="ADX calculation period")
    adx_threshold: int = Field(default=25, ge=1, le=100, description="Minimum ADX for trend strength")
    atr_period: int = Field(default=14, ge=1, le=50, description="ATR calculation period")
    atr_multiplier_stop: float = Field(default=3.0, ge=0.5, le=10.0, description="ATR multiplier for trailing stop")
    atr_default_pct: float = Field(default=0.02, ge=0.001, le=0.5, description="Default ATR as fraction of price")
    trade_capital_usd: int = Field(default=5, ge=1, description="Capital per trade in USD")
    tp_capital_pct: float = Field(default=50.0, ge=0.0, le=100.0, description="Take-profit as percentage of trade capital")
    confidence_default: float = Field(default=0.7, ge=0.0, le=1.0, description="Default confidence for signals")
    confidence_high: float = Field(default=0.9, ge=0.0, le=1.0, description="High confidence for strong signals")


class RateLimiterConfig(BaseModel):
    """Rate limiter sliding window configuration for the Groq API."""

    window_seconds: int = Field(
        default=86400,
        ge=60,
        description="Rate limiter sliding window in seconds (default 24h)",
    )
    warning_level_1: int = Field(
        default=800,
        ge=1,
        description="First rate-limit warning level (number of requests)",
    )
    warning_level_2: int = Field(
        default=900,
        ge=1,
        description="Second rate-limit warning level (number of requests)",
    )
    warning_level_3: int = Field(
        default=950,
        ge=1,
        description="Third rate-limit warning level (number of requests)",
    )


class TokenBudgetConfig(BaseModel):
    """Daily token-budget throttle for the Groq API.

    The Groq free tier is quota-bound by TOKENS per day, not requests
    (the old 70b model burned exactly this: ``429 tokens per day: Limit
    100000, used 97364``).  ``GroqClient`` estimates tokens per request
    with a dependency-light chars/4 heuristic and refuses / throttles once
    the rolling daily token usage is exhausted, instead of burning HTTP
    429s.  The request-based :class:`RateLimiterConfig` is unchanged and
    continues to apply alongside this.
    """

    enabled: bool = Field(
        default=True,
        description="Enable the daily token-budget throttle",
    )
    max_tokens_per_day: int = Field(
        default=500_000,
        ge=1,
        description=(
            "Daily token budget (default matches llama-3.1-8b-instant "
            "free tier: 500K tokens/day)"
        ),
    )
    window_seconds: int = Field(
        default=86400,
        ge=60,
        description="Rolling token-usage window in seconds (default 24h)",
    )
    warning_level_1: int = Field(
        default=400_000,
        ge=1,
        description="First warning level (estimated tokens used in window)",
    )
    warning_level_2: int = Field(
        default=450_000,
        ge=1,
        description="Second warning level (estimated tokens used in window)",
    )
    warning_level_3: int = Field(
        default=480_000,
        ge=1,
        description="Third warning level (estimated tokens used in window)",
    )


class GroqClientConfig(BaseModel):
    """Groq LLM client configuration.

    Controls retry/backoff, rate-limiter warning levels, the daily
    token-budget throttle, and fallback action logic.
    """

    timeout_seconds: float = Field(
        default=30.0,
        ge=5.0,
        le=120.0,
        description="Groq API request timeout in seconds",
    )
    max_retries: int = Field(
        default=3,
        ge=0,
        description="Maximum Groq API request retries",
    )
    base_backoff_seconds: float = Field(
        default=1.0,
        ge=0.1,
        description="Base backoff in seconds for Groq API retry",
    )
    rate_limiter: RateLimiterConfig = Field(
        default_factory=RateLimiterConfig,
        description="Groq API rate limiter configuration",
    )
    token_budget: TokenBudgetConfig = Field(
        default_factory=TokenBudgetConfig,
        description="Daily token-budget throttle for the Groq API",
    )
    valid_actions: list[str] = Field(
        default_factory=lambda: ["ENTER", "EXIT", "HOLD", "adjust_stop", "reduce_position"],
        description="Valid AI trading actions",
    )
    fallback_action: str = Field(
        default="HOLD",
        description="Fallback trading action when AI decision fails",
    )


class PromptBuilderConfig(BaseModel):
    """AI prompt builder defaults for trading analysis prompts."""

    order_book_depth: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Order book depth levels to include in prompts",
    )
    max_candles: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Max candles for compact display in prompts",
    )


class AiRotationConfig(BaseModel):
    """Per-pair rotation: trade one pair at a time, rotate on position close."""

    enabled: bool = Field(
        default=False,
        description="Trade one pair at a time; advance only after the position closes",
    )
    retry_sleep_seconds: float = Field(
        default=30.0,
        ge=0.0,
        description="Sleep between HOLD scans of successive pairs (seconds)",
    )


# ============================================================================
# AI Section
# ============================================================================

class AiValidatorConfig(BaseModel):
    """Deterministic direction-to-side validation of AI decisions.

    Phase 1 inversion guard: the LLM forecasts a direction and the bot
    derives the order side deterministically.  ``gate_mode`` controls how
    the technical plausibility gate treats entries that fight the trend.
    """

    gate_mode: Literal["warn", "veto"] = Field(
        default="warn",
        description=(
            "Plausibility gate behaviour: 'warn' logs a trend/RSI veto "
            "without rejecting; 'veto' rejects the decision"
        ),
    )
    min_confidence_to_trade: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum confidence (0-1) for ENTER/EXIT decisions.  Decisions "
            "below this threshold are rejected (downgraded to a safe HOLD by "
            "the orchestrator).  0.0 disables the gate."
        ),
    )


class AiMetricsConfig(BaseModel):
    """Prediction-quality metrics computation for AI decisions.

    Phase 3: the orchestrator periodically pulls resolved decisions
    (``outcome != 'open'``) and feeds them to ``quad.ai.metrics`` to compute
    hit rate, Expected Calibration Error, and Brier score.  This section gates
    whether the computation runs and how often.
    """

    enabled: bool = Field(
        default=True,
        description="Compute and log AI decision-quality metrics each interval",
    )
    interval_cycles: int = Field(
        default=1,
        ge=1,
        description="Compute metrics every N main cycles (1 = every cycle)",
    )
    min_resolved: int = Field(
        default=5,
        ge=0,
        description="Minimum resolved directional rows before logging metrics",
    )
    only_directional: bool = Field(
        default=True,
        description="Restrict the metrics query to LONG/SHORT resolved rows",
    )


class AiConfig(BaseModel):
    """AI-driven trading analysis and signal generation configuration."""

    api_key: str | None = Field(
        default=None,
        description="Groq API key (overrides GROQ_API_KEY env var)",
    )
    enabled: bool = Field(
        default=True,
        description="Enable AI-driven trading analysis",
    )
    model: str = Field(
        default="llama-3.1-8b-instant",
        description="Groq LLM model identifier",
    )
    timeout: int = Field(
        default=30,
        ge=5,
        le=120,
        description="LLM API request timeout in seconds",
    )
    temperature: float = Field(
        default=0.0,
        ge=0.0,
        le=2.0,
        description="LLM sampling temperature",
    )
    max_tokens: int = Field(
        default=2048,
        ge=64,
        le=8192,
        description="Default max tokens for AI chat completions",
    )
    default_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Default confidence value when AI decision does not provide one",
    )
    max_requests_per_day: int = Field(
        default=950,
        ge=1,
        le=10000,
        description="Maximum LLM API requests per day",
    )
    pairs: list[str] = Field(
        default_factory=lambda: ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"],
        description="Trading pairs the AI module monitors",
    )
    timeframes: list[str] = Field(
        default_factory=lambda: ["15m", "1h"],
        description="Timeframes for AI technical analysis",
    )
    candle_count: int = Field(
        default=300,
        ge=50,
        le=1000,
        description="Number of historical candles per analysis",
    )
    system_prompt_override: str | None = Field(
        default=None,
        description="Optional override for the AI system prompt",
    )
    groq: GroqClientConfig = Field(
        default_factory=GroqClientConfig,
        description="Groq LLM client configuration",
    )
    prompt: PromptBuilderConfig = Field(
        default_factory=PromptBuilderConfig,
        description="AI prompt builder configuration",
    )
    rotation: AiRotationConfig = Field(
        default_factory=AiRotationConfig,
        description="Per-pair rotation configuration",
    )
    validator: AiValidatorConfig = Field(
        default_factory=AiValidatorConfig,
        description="Deterministic direction-to-side decision validation",
    )
    metrics: AiMetricsConfig = Field(
        default_factory=AiMetricsConfig,
        description="Prediction-quality metrics computation (hit rate, ECE, Brier)",
    )


# ============================================================================
# TradingView Webhook Section
# ============================================================================

class TradingViewWebhookConfig(BaseModel):
    """TradingView webhook receiver configuration."""

    enabled: bool = Field(
        default=False,
        description="Enable the TradingView webhook receiver",
    )
    port: int = Field(
        default=9090,
        ge=1024,
        le=65535,
        description="Port for the webhook HTTP server",
    )
    secret: str = Field(
        default="",
        description="Shared secret for webhook HMAC signature verification",
    )

    @model_validator(mode="after")
    def validate_secret_when_enabled(self) -> TradingViewWebhookConfig:
        """Require a non-empty secret when the webhook is enabled."""
        if self.enabled and (not self.secret or len(self.secret.strip()) < 16):
            raise ValueError(
                "tradingview_webhook.secret must be at least 16 characters "
                "when tradingview_webhook.enabled is true"
            )
        return self


# ============================================================================
# Retrain / Self-Optimization Section
# ============================================================================


class RetrainConfig(BaseModel):
    """Configuration for the 7-day strategy self-optimization cycle.

    Runs periodically to analyze past trading decisions vs. outcomes and
    adjust strategy parameters, risk thresholds, or AI prompts based on
    performance data.
    """

    enabled: bool = Field(
        default=False,
        description="Enable the optimisation cycle",
    )
    interval_days: int = Field(
        default=7,
        ge=1,
        le=90,
        description="Days between optimisation runs",
    )
    initial_delay_hours: int = Field(
        default=1,
        ge=0,
        le=168,
        description="Hours to wait after bot startup before first optimisation run",
    )
    min_trades_for_analysis: int = Field(
        default=10,
        ge=1,
        description="Minimum trades in the period to produce recommendations",
    )
    max_history_days: int = Field(
        default=90,
        ge=1,
        le=365,
        description="How far back (days) to pull data for analysis",
    )
    confidence_threshold: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Minimum confidence score to auto-apply a recommendation",
    )
    auto_apply: bool = Field(
        default=False,
        description="Apply high-confidence recommendations without manual approval",
    )
    max_recommendations_per_cycle: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Cap on recommendations per cycle",
    )
    groq_temperature: float = Field(
        default=0.2,
        ge=0.0,
        le=1.0,
        description="Temperature for optimisation analysis calls",
    )
    groq_max_tokens: int = Field(
        default=2048,
        ge=512,
        le=8192,
        description="Max tokens for optimisation analysis calls",
    )
    analysis_prompt_override: str | None = Field(
        default=None,
        description="Override the default optimisation system prompt",
    )


# ============================================================================
# Execution Section
# ============================================================================


class ExecutionConfig(BaseModel):
    """Order execution engine configuration."""

    reconcile_interval_seconds: int = Field(
        default=60, ge=5,
        description="Seconds between order reconciliation cycles",
    )
    twap_window_seconds: int = Field(
        default=300, ge=10,
        description="Default TWAP execution window in seconds",
    )
    default_order_type: str = Field(
        default="MARKET",
        description="Default order type (MARKET only; limit orders disabled)",
    )
    reduce_only: bool = Field(
        default=False,
        description="Default reduce-only flag for orders",
    )
    post_only: bool = Field(
        default=False,
        description="Default post-only flag for orders",
    )
    twap: dict[str, Any] = Field(
        default_factory=lambda: {
            "min_slices": 3,
            "max_slices": 10,
            "jitter_seconds": 5,
            "min_slice_quantity": 0.01,
            "fill_urgency_threshold": 0.8,
        },
        description="TWAP slicer parameters (min_slices, max_slices, etc.)",
    )


# ============================================================================
# Backtesting Section
# ============================================================================


class BacktestConfig(BaseModel):
    """Backtest engine simulation parameters."""

    starting_capital: float = Field(
        default=10000.0, ge=100.0,
        description="Starting capital for backtest simulations in USD",
    )
    commission_pct: float = Field(
        default=0.001, ge=0.0, le=0.1,
        description="Commission as a fraction of trade value per side",
    )
    slippage_pct: float = Field(
        default=0.0005, ge=0.0, le=0.1,
        description="Slippage as a fraction of price per fill",
    )
    max_trades_per_day: int = Field(
        default=10, ge=1, le=1000,
        description="Maximum trades per day in simulation",
    )


# ============================================================================
# Root Config Model
# ============================================================================

class QuadConfig(BaseModel):
    """Root configuration model for the Quad trading bot.

    Validates all configuration sections and their interdependencies.
    Use `validate_config()` for a convenience wrapper that returns error lists.
    """

    execution: ExecutionConfig = Field(
        default_factory=ExecutionConfig,
        description="Execution engine configuration",
    )
    trading: TradingConfig = Field(
        default_factory=TradingConfig,
        description="Trading behavior configuration",
    )
    exchange: ExchangeConfig = Field(
        default_factory=ExchangeConfig,
        description="Exchange connection configuration",
    )
    risk: RiskConfig = Field(
        default_factory=RiskConfig,
        description="Risk management configuration",
    )
    ai: AiConfig = Field(
        default_factory=AiConfig,
        description="AI-driven trading analysis configuration",
    )
    market_data: MarketDataConfig = Field(
        default_factory=MarketDataConfig,
        description="Market data engine configuration",
    )
    persistence: PersistenceConfig = Field(
        default_factory=PersistenceConfig,
        description="Database persistence configuration",
    )
    telegram: TelegramConfig = Field(
        default_factory=TelegramConfig,
        description="Telegram bot configuration",
    )
    tradingview_webhook: TradingViewWebhookConfig = Field(
        default_factory=TradingViewWebhookConfig,
        description="TradingView webhook receiver configuration",
    )
    retrain: RetrainConfig = Field(
        default_factory=RetrainConfig,
        description="7-day strategy self-optimisation cycle configuration",
    )
    monitoring: MonitoringConfig = Field(
        default_factory=MonitoringConfig,
        description="Monitoring and health check configuration",
    )
    backtesting: BacktestConfig = Field(
        default_factory=BacktestConfig,
        description="Backtest engine simulation parameters",
    )
    strategy: dict[str, Any] = Field(
        default_factory=lambda: {
            "trend_following": TrendFollowingParams().model_dump(),
        },
        description="Strategy-specific parameters",
    )

    model_config = {"extra": "allow"}


# ============================================================================
# Public Validation API
# ============================================================================

def validate_config(
    config_dict: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Validate a configuration dictionary against the QuadConfig schema.

    This is the primary entry point for validating YAML config files
    before they are loaded by ConfigManager.

    Args:
        config_dict: Raw configuration dictionary (e.g., from yaml.safe_load).

    Returns:
        Tuple of (is_valid, error_messages). If valid, error_messages is empty.
        If invalid, error_messages contains human-readable validation errors.
    """
    errors: list[str] = []

    try:
        QuadConfig.model_validate(config_dict)
        return True, errors
    except Exception as exc:  # noqa: BLE001
        # Pydantic raises ValidationError which has .errors() for structured
        # access, but we catch broadly to handle any validation issue.
        if hasattr(exc, "errors"):
            for err in exc.errors():  # type: ignore[union-attr]
                field_path = " -> ".join(
                    str(loc) for loc in err.get("loc", [])
                )
                msg = err.get("msg", str(err))
                errors.append(f"{field_path}: {msg}")
        else:
            errors.append(str(exc))

    return False, errors
