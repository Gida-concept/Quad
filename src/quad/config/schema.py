"""Configuration schema validation using Pydantic v2.

Defines expected types, ranges, and constraints for all configuration keys
across trading, exchange, risk, market data, persistence, logging, telegram,
monitoring, and strategy sections.

Usage:
    from quad.config.schema import validate_config

    with open("config/config.default.yaml") as f:
        raw = yaml.safe_load(f)
    is_valid, errors = validate_config(raw)
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# ============================================================================
# Trading Section
# ============================================================================

class TradingConfig(BaseModel):
    """Configuration for trading behavior and parameters."""

    default_strategy: str = Field(
        default="cash_secured_put",
        description="Default strategy name for the bot",
    )
    max_positions: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum number of concurrent positions",
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
        default_factory=lambda: ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"],
        description="List of underlying assets the bot monitors",
    )
    preferred_expiry: str = Field(
        default="weekly",
        description="Preferred option expiry cycle: weekly, monthly, or quarterly",
    )
    min_dte: int = Field(
        default=7,
        ge=0,
        description="Minimum days to expiry for option selection",
    )
    max_dte: int = Field(
        default=45,
        ge=1,
        le=365,
        description="Maximum days to expiry for option selection",
    )

    @field_validator("preferred_expiry")
    @classmethod
    def validate_preferred_expiry(cls, value: str) -> str:
        """Validate that preferred_expiry is one of the supported values."""
        allowed = {"weekly", "monthly", "quarterly"}
        if value.lower() not in allowed:
            raise ValueError(
                f"preferred_expiry must be one of {allowed}, got '{value}'"
            )
        return value.lower()

    @field_validator("max_dte")
    @classmethod
    def validate_dte_range(cls, value: int, info: Any) -> int:
        """Ensure max_dte >= min_dte when both are available."""
        data = info.data  # type: ignore[union-attr]
        if "min_dte" in data and value < data["min_dte"]:
            raise ValueError(
                f"max_dte ({value}) must be >= min_dte ({data['min_dte']})"
            )
        return value


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


class WebSocketConfig(BaseModel):
    """WebSocket connection configuration."""

    ping_interval: int = Field(
        default=30,
        ge=5,
        le=300,
        description="Seconds between WebSocket keep-alive pings",
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
    websocket: WebSocketConfig = Field(
        default_factory=WebSocketConfig,
        description="WebSocket configuration",
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """Validate exchange name is supported."""
        allowed = {"binance", "paper", "mock"}
        if value.lower() not in allowed:
            raise ValueError(
                f"exchange name must be one of {allowed}, got '{value}'"
            )
        return value.lower()


# ============================================================================
# Risk Section
# ============================================================================

class CircuitBreakerConfig(BaseModel):
    """Circuit breaker threshold configuration."""

    drawdown_tiers: list[float] = Field(
        default=[5.0, 10.0, 15.0],
        description="Drawdown percentage tiers for escalating responses",
    )
    greek_limits: dict[str, float] = Field(
        default_factory=lambda: {"delta": 15.0, "gamma": 5.0, "vega": 1000.0},
        description="Hard Greek exposure limits",
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


class StopLossConfig(BaseModel):
    """Stop-loss configuration."""

    enabled: bool = Field(default=True, description="Enable stop-loss")
    portfolio_pct: float = Field(
        default=15.0,
        ge=0.0,
        le=100.0,
        description="Portfolio percentage to trigger stop-loss",
    )


class TakeProfitConfig(BaseModel):
    """Take-profit configuration."""

    enabled: bool = Field(default=True, description="Enable take-profit")
    portfolio_pct: float = Field(
        default=30.0,
        ge=0.0,
        le=100.0,
        description="Portfolio percentage to trigger take-profit",
    )


class RiskConfig(BaseModel):
    """Risk management thresholds and limits."""

    max_position_size: int = Field(
        default=5,
        ge=1,
        description="Maximum contracts per single position",
    )
    max_portfolio_risk_pct: float = Field(
        default=10.0,
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
        default=20.0,
        ge=0.0,
        le=100.0,
        description="Max drawdown from peak portfolio value",
    )
    max_delta_exposure: float = Field(
        default=10.0,
        description="Aggregate delta limit across all positions",
    )
    max_theta_decay: float = Field(
        default=-100.0,
        description="Maximum daily theta decay (negative value)",
    )
    max_vega_exposure: float = Field(
        default=500.0,
        description="Aggregate vega exposure limit",
    )
    iv_percentile_min: float = Field(
        default=10.0,
        ge=0.0,
        le=100.0,
        description="Minimum IV percentile for trade entry",
    )
    iv_percentile_max: float = Field(
        default=90.0,
        ge=0.0,
        le=100.0,
        description="Maximum IV percentile for trade entry",
    )
    concentration_limit_pct: float = Field(
        default=30.0,
        ge=0.0,
        le=100.0,
        description="Max percentage of portfolio in a single underlying",
    )
    circuit_breakers: CircuitBreakerConfig = Field(
        default_factory=CircuitBreakerConfig,
        description="Circuit breaker thresholds",
    )
    stop_loss: StopLossConfig = Field(
        default_factory=StopLossConfig,
        description="Stop-loss configuration",
    )
    take_profit: TakeProfitConfig = Field(
        default_factory=TakeProfitConfig,
        description="Take-profit configuration",
    )

    @field_validator("iv_percentile_max")
    @classmethod
    def validate_iv_percentile_range(cls, value: float, info: Any) -> float:
        """Ensure iv_percentile_max >= iv_percentile_min."""
        data = info.data  # type: ignore[union-attr]
        if "iv_percentile_min" in data and value < data["iv_percentile_min"]:
            raise ValueError(
                f"iv_percentile_max ({value}) must be >= "
                f"iv_percentile_min ({data['iv_percentile_min']})"
            )
        return value


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
    greeks: int = Field(
        default=500,
        ge=50,
        description="Number of Greek snapshots to buffer per symbol",
    )


class CacheTTLConfig(BaseModel):
    """Cache time-to-live values."""

    option_chain: int = Field(
        default=30,
        ge=1,
        le=3600,
        description="Option chain cache TTL in seconds",
    )
    underlying_price: int = Field(
        default=5,
        ge=1,
        le=60,
        description="Underlying price cache TTL in seconds",
    )


class HistoricalConfig(BaseModel):
    """Historical data access configuration."""

    max_days: int = Field(
        default=90,
        ge=1,
        le=365,
        description="Max days of historical data to cache",
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
    historical: HistoricalConfig = Field(
        default_factory=HistoricalConfig,
        description="Historical data configuration",
    )


# ============================================================================
# Persistence Section
# ============================================================================

class BackupConfig(BaseModel):
    """Database backup configuration."""

    enabled: bool = Field(default=True, description="Enable automatic backups")
    interval: int = Field(
        default=3600,
        ge=60,
        description="Seconds between automatic backups",
    )
    max_count: int = Field(
        default=24,
        ge=1,
        le=365,
        description="Maximum number of backup files to retain",
    )


class PersistenceConfig(BaseModel):
    """Database persistence configuration."""

    dsn: str = Field(
        default="${DATABASE_URL:-postgresql://quad:quad@localhost:5432/quad}",
        description="PostgreSQL connection DSN",
    )
    busy_timeout: int = Field(
        default=5000,
        ge=0,
        le=60000,
        description="Connection pool timeout in milliseconds",
    )
    snapshot_interval: int = Field(
        default=60,
        ge=5,
        le=86400,
        description="Seconds between database snapshots",
    )
    backup: BackupConfig = Field(
        default_factory=BackupConfig,
        description="Backup configuration",
    )


# ============================================================================
# Logging Section
# ============================================================================

class LogFileConfig(BaseModel):
    """Log file output configuration."""

    enabled: bool = Field(default=True, description="Enable file logging")
    path: str = Field(
        default="${QUAD_LOG_DIR}/quad.log",
        description="Log file path",
    )
    max_size_mb: int = Field(
        default=100,
        ge=1,
        le=1024,
        description="Maximum log file size in MB before rotation",
    )
    backup_count: int = Field(
        default=10,
        ge=0,
        le=100,
        description="Number of rotated log files to retain",
    )


class LoggingConfig(BaseModel):
    """Logging configuration."""

    level: str = Field(
        default="INFO",
        description="Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL",
    )
    format: str = Field(
        default="json",
        description="Log output format: json or console",
    )
    file: LogFileConfig = Field(
        default_factory=LogFileConfig,
        description="File logging configuration",
    )

    @field_validator("level")
    @classmethod
    def validate_level(cls, value: str) -> str:
        """Validate log level is a standard Python logging level."""
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(
                f"log level must be one of {allowed}, got '{value}'"
            )
        return upper

    @field_validator("format")
    @classmethod
    def validate_format(cls, value: str) -> str:
        """Validate log format is supported."""
        allowed = {"json", "console"}
        lower = value.lower()
        if lower not in allowed:
            raise ValueError(
                f"log format must be one of {allowed}, got '{value}'"
            )
        return lower


# ============================================================================
# Telegram Section
# ============================================================================

class TelegramConfig(BaseModel):
    """Telegram bot integration configuration."""

    enabled: bool = Field(default=True, description="Enable Telegram bot")
    admin_ids: list[int] = Field(
        default_factory=list,
        description="Telegram user IDs authorized for admin commands",
    )
    polling: bool = Field(
        default=True,
        description="Use long-polling (true) or webhook (false)",
    )

    @field_validator("admin_ids", mode="before")
    @classmethod
    def coerce_admin_ids(cls, value: Any) -> list[int]:
        """Coerce single admin ID or comma-separated string to list."""
        if isinstance(value, str):
            if not value.strip():
                return []
            return [int(x.strip()) for x in value.split(",") if x.strip()]
        if isinstance(value, int):
            return [value]
        if isinstance(value, (list, tuple)):
            result: list[int] = []
            for item in value:
                if isinstance(item, str):
                    result.append(int(item.strip()))
                else:
                    result.append(int(item))
            return result
        return list(value) if value else []


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

class BaseStrategyParams(BaseModel):
    """Base parameters shared across strategies."""

    min_dte: int = Field(default=7, ge=1, le=365)
    max_dte: int = Field(default=45, ge=1, le=365)


class CoveredCallParams(BaseStrategyParams):
    """Parameters for the covered call strategy."""

    delta_target: float = Field(default=0.30, ge=0.0, le=1.0)
    roll_when_dte_lt: int = Field(default=3, ge=1)
    allocation_pct: float = Field(default=0.5, ge=0.0, le=1.0)


class CashSecuredPutParams(BaseStrategyParams):
    """Parameters for the cash-secured put strategy."""

    delta_target: float = Field(default=0.25, ge=0.0, le=1.0)
    roll_when_dte_lt: int = Field(default=3, ge=1)
    cash_reserve_pct: float = Field(default=0.3, ge=0.0, le=1.0)


class IronCondorParams(BaseStrategyParams):
    """Parameters for the iron condor strategy."""

    wing_delta_target: float = Field(default=0.15, ge=0.0, le=1.0)
    wing_width_pct: float = Field(default=10.0, ge=0.0, le=100.0)
    min_dte: int = Field(default=14, ge=1, le=365)
    max_dte: int = Field(default=45, ge=1, le=365)


class StraddleParams(BaseStrategyParams):
    """Parameters for the straddle strategy."""

    delta_target: float = Field(default=0.50, ge=0.0, le=1.0)
    max_dte: int = Field(default=30, ge=1, le=365)
    min_iv_percentile: float = Field(default=50.0, ge=0.0, le=100.0)


class StrangleParams(BaseStrategyParams):
    """Parameters for the strangle strategy."""

    wing_delta_target: float = Field(default=0.20, ge=0.0, le=1.0)
    min_dte: int = Field(default=14, ge=1, le=365)
    max_dte: int = Field(default=45, ge=1, le=365)
    min_iv_percentile: float = Field(default=50.0, ge=0.0, le=100.0)


class VerticalSpreadParams(BaseStrategyParams):
    """Parameters for the vertical spread strategy."""

    long_leg_delta: float = Field(default=0.30, ge=0.0, le=1.0)
    wing_width_pct: float = Field(default=5.0, ge=0.0, le=100.0)
    direction: str = Field(
        default="neutral",
        description="Spread direction: bullish, bearish, or neutral",
    )

    @field_validator("direction")
    @classmethod
    def validate_direction(cls, value: str) -> str:
        """Validate direction is one of the supported values."""
        allowed = {"bullish", "bearish", "neutral"}
        lower = value.lower()
        if lower not in allowed:
            raise ValueError(
                f"direction must be one of {allowed}, got '{value}'"
            )
        return lower


# ============================================================================
# AI Section
# ============================================================================

class AiConfig(BaseModel):
    """AI-driven trading analysis and signal generation configuration."""

    enabled: bool = Field(
        default=True,
        description="Enable AI-driven trading analysis",
    )
    model: str = Field(
        default="llama-3.3-70b-versatile",
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
    max_requests_per_day: int = Field(
        default=950,
        ge=1,
        le=10000,
        description="Maximum LLM API requests per day",
    )
    fallback_on_error: bool = Field(
        default=True,
        description="Fall back to rule-based signals on LLM error",
    )
    structured_output: bool = Field(
        default=True,
        description="Request structured JSON output from the LLM",
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
    min_open_interest: int = Field(
        default=50,
        ge=0,
        description="Minimum open interest (USD) for symbol inclusion",
    )
    system_prompt_override: str | None = Field(
        default=None,
        description="Optional override for the AI system prompt",
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
# Root Config Model
# ============================================================================

class QuadConfig(BaseModel):
    """Root configuration model for the Quad trading bot.

    Validates all configuration sections and their interdependencies.
    Use `validate_config()` for a convenience wrapper that returns error lists.
    """

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
    logging: LoggingConfig = Field(
        default_factory=LoggingConfig,
        description="Logging configuration",
    )
    telegram: TelegramConfig = Field(
        default_factory=TelegramConfig,
        description="Telegram bot configuration",
    )
    tradingview_webhook: TradingViewWebhookConfig = Field(
        default_factory=TradingViewWebhookConfig,
        description="TradingView webhook receiver configuration",
    )
    monitoring: MonitoringConfig = Field(
        default_factory=MonitoringConfig,
        description="Monitoring and health check configuration",
    )
    strategy: dict[str, Any] = Field(
        default_factory=lambda: {
            "covered_call": CoveredCallParams().model_dump(),
            "cash_secured_put": CashSecuredPutParams().model_dump(),
            "iron_condor": IronCondorParams().model_dump(),
            "straddle": StraddleParams().model_dump(),
            "strangle": StrangleParams().model_dump(),
            "vertical_spread": VerticalSpreadParams().model_dump(),
        },
        description="Strategy-specific parameters",
    )


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
