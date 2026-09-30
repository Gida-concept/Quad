# API Reference

---

## Architecture Overview

Quad is a single-process Python application. All internal interfaces are defined as Python abstract base classes (ABCs) or protocols. The public API consists of:

1. **CLI commands** (Typer) -- User-facing interface
2. **Plugin interfaces** (ABCs) -- For strategy and exchange adapter developers
3. **Health check HTTP server** -- For Docker and monitoring integration

---

## Plugin Interfaces

### ExchangeAdapter ABC

All exchange integrations implement this interface. Located in `src/quad/exchange/base.py`.

```python
from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional
from decimal import Decimal

class ExchangeAdapter(ABC):
    """Abstract base for exchange integrations."""

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to the exchange."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully close all connections."""
        ...

    @abstractmethod
    async def get_account(self) -> Account:
        """Get account information and balances."""
        ...

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Get all open positions."""
        ...

    @abstractmethod
    async def get_funding_rate(self, symbol: str) -> FundingRate:
        """Get current funding rate for a symbol."""
        ...

    @abstractmethod
    async def get_order_book(self, symbol: str, limit: int = 50) -> OrderBook:
        """Get current order book for a symbol."""
        ...

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        """Set leverage for a symbol."""
        ...

    @abstractmethod
    async def set_margin_type(
        self, symbol: str, margin_type: MarginType
    ) -> dict:
        """Set margin type (ISOLATED/CROSS) for a symbol."""
        ...

    @abstractmethod
    async def place_order(self, order: OrderRequest) -> OrderResult:
        """Place an order on the exchange."""
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        ...

    @abstractmethod
    async def get_order_status(self, order_id: str) -> OrderStatus:
        """Get the current status of an order."""
        ...

    @abstractmethod
    async def subscribe_account_updates(self) -> AsyncIterator[AccountUpdate]:
        """Subscribe to account balance and futures position updates."""
        ...
```

### Strategy ABC

All trading strategies implement this interface. Located in `src/quad/strategy/base.py`.

```python
from abc import ABC, abstractmethod
from typing import Optional

class StrategyBase(ABC):
    """Abstract base for trading strategies."""

    @staticmethod
    @abstractmethod
    def get_name() -> str:
        """Return the unique machine-readable name for this strategy."""
        ...

    @staticmethod
    @abstractmethod
    def get_description() -> str:
        """Return a human-readable description of this strategy."""
        ...

    @staticmethod
    @abstractmethod
    def get_params_spec() -> list[ParamSpec]:
        """Return the parameter specification for this strategy.

        Returns list of ParamSpec dataclass instances defining each
        configurable parameter (name, type, default, description, range).
        """
        ...

    @abstractmethod
    async def evaluate(self, context: StrategyContext) -> list[Action]:
        """Evaluate the strategy against the current context.

        Called once per trading cycle. Returns a list of Action objects
        (ENTER, EXIT, HOLD, adjust_stop, reduce_position).
        """
        ...
```

### StrategyContext

The context object passed to strategies, providing access to market data and account info.

```python
@dataclass
class StrategyContext:
    """Context provided to strategies during evaluation."""

    # Account information
    account: Account
    positions: list[Position]
    futures_positions: list[Position]
    orders: list[Order]

    # Market data
    funding_rates: dict[str, FundingRate]
    mark_prices: dict[str, float]
    candles: dict[str, list]
    order_books: dict[str, dict]

    # Risk state
    risk_status: RiskStatus | None

    # Configuration
    config: dict
    strategy_params: dict

    # Historical data access
    historical: HistoricalDataAccess | None
```

---

## Repository Interfaces

Data access follows the repository pattern. Located in src/quad/persistence/repositories.py.

```python
class BaseRepository[T]:
    """Generic CRUD base."""

    async def get(self, id: str) -> Optional[T]: ...
    async def list(self, filters: dict = None) -> list[T]: ...
    async def create(self, entity: T) -> T: ...
    async def update(self, entity: T) -> T: ...
    async def delete(self, id: str) -> bool: ...

class AccountRepository(BaseRepository[Account]):
    async def get_by_exchange(self, exchange: str) -> Optional[Account]: ...
    async def update_balance(self, account_id: str, balance: Decimal) -> None: ...

class PositionRepository(BaseRepository[Position]):
    async def get_open(self) -> list[Position]: ...
    async def get_by_strategy(self, strategy: str) -> list[Position]: ...
    async def get_by_symbol(self, symbol: str) -> list[Position]: ...
    async def get_open_futures_positions(self) -> list[Position]: ...
    async def close(self, position_id: str, pnl: Decimal) -> None: ...

class OrderRepository(BaseRepository[Order]):
    async def get_open(self) -> list[Order]: ...
    async def get_by_position(self, position_id: str) -> list[Order]: ...
    async def update_status(self, order_id: str, status: str) -> None: ...

class TradeRepository(BaseRepository[Trade]):
    async def get_by_position(self, position_id: str) -> list[Trade]: ...
    async def get_recent(self, limit: int = 50) -> list[Trade]: ...

class FundingRepository(BaseRepository[FundingPayment]):
    async def get_funding_history(self, symbol: str) -> list[FundingPayment]: ...
    async def get_total_funding_paid(self, symbol: str) -> Decimal: ...

class LiquidationRepository(BaseRepository[LiquidationEvent]):
    async def get_recent(self, limit: int = 50) -> list[LiquidationEvent]: ...
```

---

## Risk Manager Interface

Located in `src/quad/risk/manager.py`.

```python
class RiskManager:
    """Coordinates all risk checks and circuit breakers."""

    async def check_trade(self, action: Action, context: StrategyContext) -> RiskResult:
        """Run all 9 pre-trade checks. Returns PASS or FAIL with reason."""
        ...

    async def check_margin(self, order: OrderRequest, account: Account) -> RiskResult:
        """Check margin sufficiency for an order."""
        ...

    async def get_status(self) -> RiskStatus:
        """Get current risk management status."""
        ...

    async def check_circuit_breakers(self, portfolio: PortfolioState) -> BreakerStatus:
        """Check all 7 circuit breaker types."""
        ...
```

---

## Health Check HTTP Server

A lightweight HTTP server for Docker health checks and monitoring. Located at `src/quad/monitoring/health.py`.

**Base URL:** `http://127.0.0.1:9090` (configurable via `monitoring.health_server.port` or `QUAD_HEALTH_PORT`)

| Method | Path | Description |
|---|---|---|
| GET | `/health` (and `/`) | Overall health: status, uptime, version, per-component readiness |
| GET | `/readiness` (alias `/ready`) | Component readiness map |
| GET | `/liveness` (alias `/live`) | Liveness check (is process alive?) |
| GET | `/metrics` | Prometheus-formatted operational metrics |
| POST | `/webhook/tradingview` | TradingView alert receiver (only when `tradingview_webhook.enabled`) |

> **This is the bot's own health server** (started by the orchestrator inside
> the trading process, default port 9090). It is *not* the `quad-api` FastAPI
> service, which exposes a separate pair of endpoints -- see
> [API Service Health Endpoints](#api-service-health-endpoints) below.

> **Authentication.** With no API key configured the server binds to
> `127.0.0.1` regardless of `bind_address` and only accepts direct loopback
> requests. The loopback bypass is additionally **refused when
> reverse-proxy forwarding headers are present** (`X-Forwarded-For`,
> `X-Real-IP`, `X-Forwarded-Host`, `Forwarded`), because behind a proxy
> `request.remote` is the proxy itself. If you expose this server through a
> reverse proxy you must set `QUAD_HEALTH_API_KEY` and send `X-API-Key`,
> or every request (including your own healthcheck) returns 403.
> Unauthenticated requests return 403, not 401.

### GET /health

```json
{
  "status": "ok",
  "uptime": 86400.5,
  "version": "0.1.0",
  "timestamp": 1759000000000,
  "components": {
    "config": true,
    "database": true,
    "exchange": true,
    "market_data": true,
    "risk_manager": true,
    "execution_engine": true
  },
  "degraded": []
}
```

`status` is `"degraded"` (and exit code 1 from `quad health`) when any
component reports false. Each component check is fail-closed: an exception
in a check counts as unhealthy, not healthy.

### GET /readiness (or `/ready`)

```json
{
  "ready": true,
  "components": {
    "config": true,
    "database": true,
    "exchange": true
  }
}
```

### GET /metrics

Prometheus text exposition format. Note that the metrics collector does
**not** add a `quad_` prefix — names are exactly as registered by each
subsystem.

```
# HELP quad_uptime_seconds Bot uptime in seconds
# TYPE quad_uptime_seconds gauge
quad_uptime_seconds 86400.00
```

When the metrics collector is attached (it is, once the orchestrator has
finished starting), the trading loop adds these gauges and counters:

| Metric | Type | Meaning |
|---|---|---|
| `orchestrator_started` | gauge | `1` once startup completes |
| `active_positions` | gauge | Open positions at the last cycle |
| `active_strategies` | gauge | Number of active strategy instances |
| `dry_run` | gauge | `1` while `_dry_run` is set |
| `dry_run_guard_active` | gauge | `1` while dry-run is blocking orders against a **live** exchange |
| `portfolio_value` | gauge | Account total USDT equity |
| `ai_decisions` | counter | AI decisions produced |
| `trading_cycles` | counter | Completed trading cycles |
| `ai_cycle_time_ms` | gauge | Duration of the last AI cycle |

Gauges are only present after the first cycle that sets them, so a scrape
taken during startup legitimately returns fewer lines.

---

## API Service Health Endpoints

`quad-api` (the FastAPI service, `src/quad/api/app.py`) exposes its own health
endpoints. These are **unrelated to the bot's health server above** — different
process, different port, different contract. Do not point a bot healthcheck at
them.

| Method | Path | Checks dependencies | Returns 503 when unhealthy |
|---|---|---|---|
| GET | `/health` | Yes — the database (`SELECT 1`) | Yes |
| GET | `/live` | No — process liveness only | No |

**Use them like this:**

- `readinessProbe` → `/health`. An API that cannot reach its database cannot
  serve anything useful, so it should be taken out of rotation.
- `livenessProbe` → `/live`. Liveness answers "should this process be
  restarted?", and a transient database blip is **not** a reason to restart
  the API — doing so turns a small problem into an outage. `/live` therefore
  asserts nothing beyond the process serving requests.

Both require no authentication (they are probed before any token exists).

**`GET /health` response when healthy** — `200`:

```json
{
  "ok": true,
  "data": {
    "status": "ok",
    "uptime": 812.44,
    "version": "1.0.0",
    "timestamp": 1759257600000,
    "components": { "database": true },
    "degraded": []
  }
}
```

**`GET /health` response when the database is unreachable** — `503`:

```json
{
  "ok": false,
  "error": {
    "code": "unhealthy",
    "message": "unhealthy components: database"
  },
  "data": {
    "status": "degraded",
    "uptime": 812.44,
    "version": "1.0.0",
    "timestamp": 1759257600000,
    "components": { "database": false },
    "degraded": ["database"]
  }
}
```

The check is **fail-closed**: if `is_healthy()` raises (pool exhausted,
credentials rejected, driver error) the component is reported `false` and the
endpoint returns 503. An exception is never treated as healthy.

> **History.** `/health` previously returned an unconditional
> `{"status": "ok"}` without contacting any dependency, so a load balancer
> kept routing traffic to an API whose every real endpoint would fail. See
> `docs/changelog.md`.

---

## Internal Module Dependencies

```
QuadOrchestrator (orchestrator/)
  ├── ConfigManager (config/)
  ├── DatabaseManager (persistence/)
  ├── ExchangeAdapter (exchange/)
  ├── MarketDataEngine (market_data/)
  ├── RiskManager (risk/)
  │   ├── GatePipeline (risk/gates.py)
  │   ├── PositionSizer (risk/sizing.py)
  │   └── CircuitBreakerManager (risk/circuit_breakers.py)
  ├── StrategyRegistry (strategy/)
  ├── ExecutionEngine (execution/)
  │   ├── OrderGateway (execution/gateway.py)
  │   └── TWAPSplitter (execution/twap.py)
  ├── TelegramBot (bot/)
  ├── GroqClient (ai/)
  ├── HealthServer (monitoring/health.py)
  └── BacktestEngine (backtesting/)
```

The Orchestrator is the central coordinator. It wires all modules together and runs the main trading loop.
