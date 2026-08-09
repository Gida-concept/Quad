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

**Base URL:** `http://127.0.0.1:9090` (configurable)

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Service health, uptime, memory, connections |
| GET | `/ready` | Readiness check (is bot configured and running?) |
| GET | `/live` | Liveness check (is process alive?) |
| GET | `/metrics` | Prometheus-formatted operational metrics |

### GET /health

```json
{
  "status": "ok",
  "uptime": 86400,
  "version": "2.0.0",
  "python": "3.12.4",
  "memory": { "rss": 145000000, "percent": 56.6 },
  "connections": {
    "exchange": "connected",
    "database": "connected",
    "websocket": 3
  },
  "mode": "paper",
  "state": "ACTIVE",
  "positions": 2,
  "last_error": null
}
```

### GET /metrics

Exposes Prometheus-style metrics:

```
# HELP quad_uptime_seconds Bot uptime in seconds
# TYPE quad_uptime_seconds gauge
quad_uptime_seconds 86400

# HELP quad_positions_open Currently open positions
# TYPE quad_positions_open gauge
quad_positions_open 2

# HELP quad_portfolio_value_usdt Current portfolio value
# TYPE quad_portfolio_value_usdt gauge
quad_portfolio_value_usdt 10045.20

# HELP quad_drawdown_percent Current drawdown percentage
# TYPE quad_drawdown_percent gauge
quad_drawdown_percent 1.2

# HELP quad_trades_total Total trades executed
# TYPE quad_trades_total counter
quad_trades_total 12

# HELP quad_decisions_total Total decisions made
# TYPE quad_decisions_total counter
quad_decisions_total 2840

# HELP quad_errors_total Total errors
# TYPE quad_errors_total counter
quad_errors_total 3

# HELP quad_cycle_time_ms Trading cycle execution time
# TYPE quad_cycle_time_ms histogram
quad_cycle_time_ms_bucket{le="500"} 120
quad_cycle_time_ms_bucket{le="1000"} 890
quad_cycle_time_ms_bucket{le="5000"} 2840
quad_cycle_time_ms_bucket{le="+Inf"} 2840
quad_cycle_time_ms_sum 2850000
quad_cycle_time_ms_count 2840
```

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
