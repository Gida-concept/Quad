"""QuadOrchestrator — top-level application coordinator.

Wires all subsystems together and manages the full trading lifecycle:

    - Configuration loading (4-layer merge)
    - Database initialization and migrations
    - Exchange adapter creation (Bybit USDT perpetual)
    - Market data engine (WebSocket + cache + buffers)
    - Risk management (gates, circuit breakers, position sizing)
    - Strategy evaluation (all registered strategies)
    - Execution engine (order gateway, TWAP, reconciliation)
    - Telegram bot interface
    - Health check HTTP server
    - Metrics collection

Singleton pattern — exactly one orchestrator per process.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from dataclasses import replace
from typing import Any

import structlog


from quad.config.manager import ConfigManager
from quad.execution.engine import ExecutionEngine
from quad.market_data.engine import MarketDataEngine
from quad.persistence.database import DatabaseManager
from quad.risk.manager import RiskManager
from quad.strategy.base import StrategyBase
from quad.types.strategy import StrategyContext

from ._bootstrap_mixin import BootstrapMixin
from ._decisions_mixin import DecisionJournalMixin
from ._notify_mixin import NotifyMixin
from ._positions_mixin import PositionManagementMixin
from ._rotation_mixin import RotationMixin
from ._tv_mixin import TradingViewWebhookMixin

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = "config/config.yaml"

#: Accepted values for the top-level ``_mode`` / ``QUAD_MODE`` setting.
#: ``dry_run`` forces the Bybit adapter onto testnet; ``bybit`` honours
#: ``exchange.testnet``.  Anything else is a configuration error.
_VALID_MODES: frozenset[str] = frozenset({"bybit", "dry_run"})


# ============================================================================
# QuadOrchestrator
# ============================================================================


class QuadOrchestrator(
    TradingViewWebhookMixin,
    NotifyMixin,
    DecisionJournalMixin,
    BootstrapMixin,
    PositionManagementMixin,
    RotationMixin,
):
    """Main application orchestrator.

    Owns and coordinates all subsystems in a defined dependency order.
    Subsystems are created lazily in ``start()``, not in ``__init__``,
    so that construction is lightweight and ``start()`` can fail partway
    through with proper cleanup.

    Parameters
    ----------
    config_path:
        Path to the local configuration YAML file.  The directory containing
        this file is used as the ``ConfigManager`` config directory, which
        should also contain ``config.yaml``.

    Attributes
    ----------
    _mode : str
        Resolved trading mode: ``"bybit"`` or ``"dry_run"``.
    _stop_event : asyncio.Event
        Set when a shutdown signal is received.
    """

    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH) -> None:
        self._log = logger.bind()

        # Config path
        self._config_path = config_path

        # ------------------------------------------------------------------
        # Subsystems (created in start())
        # ------------------------------------------------------------------
        self._config_manager: ConfigManager | None = None
        self._db_manager: DatabaseManager | None = None
        self._exchange_adapter: Any = None
        self._market_data: MarketDataEngine | None = None
        self._risk_manager: RiskManager | None = None
        self._execution_engine: ExecutionEngine | None = None
        self._bot: Any = None
        self._health_server: Any = None
        self._metrics: Any = None
        self._active_strategies: dict[str, StrategyBase] = {}

        # Optional subsystems
        self._groq_client: Any = None
        self._optimizer: Any = None
        self._tv_webhook: Any = None
        # structlog processor that persists ERROR+ events to error_logs
        self._error_sink: Any = None

        # Cached config dict (used by multiple subsystems)
        self._config_dict: dict[str, Any] = {}
        self._mode: str = "bybit"
        self._cycle_interval: int = 60

        # AI-first mode tracking
        self._ai_cycle_interval: int = 3600
        self._ai_enabled: bool = False
        self._ai_cycle_count: int = 0
        self._last_ai_decision: dict[str, Any] = {}
        self._last_ai_error: str | None = None
        self._last_ai_cycle_time_ms: float = 0.0
        self._consecutive_ai_failures: int = 0
        # Phase 3: main-cycle counter for the config-gated metrics interval.
        self._metrics_cycle_count: int = 0

        # Pair-rotation state: trade one pair at a time, advance on close.
        self._rotation_index: int = 0  # index into ai.pairs of next pair to scan
        self._current_symbol: str = ""  # held / being-scanned pair
        # Monotonic clock of when each held symbol was first observed open,
        # used by the stale-position guard (ai.rotation.max_hold_seconds).
        self._rotation_hold_since: dict[str, float] = {}
        # AI judge cost controls (Phase 5): per-symbol last-judged closed
        # candle, plus a per-tenant daily call budget (UTC day rollover).
        self._last_judge_candle_ts: dict[str, int] = {}
        self._judge_day: str = str()
        self._judge_used: int = 0
        self._judge_budget_warned_day: str = str()
        # Scalp rotation (Phase 5): separate budget namespace + in-memory
        # registry of scalp-held symbols for the time-stop guard.
        self._scalp_day: str = str()
        self._scalp_used: int = 0
        self._scalp_budget_warned_day: str = str()
        self._scalp_held: dict[str, float] = {}
        # ``time.monotonic()`` returns a float, keyed by held symbol.
        self._last_trend_roll_ts: dict[str, float] = {}

        # Telegram notification support
        self._telegram_bot: Any = None
        self._telegram_chat_id: int = 0

        # Indicator cache (key: "SYMBOL_TIMEFRAME", invalidated every 60s)
        self._indicator_cache: dict[str, dict[str, Any]] = {}

        # Lifecycle
        self._stop_event = asyncio.Event()
        self._started = False

        # Serial-trade serialization: one lock guards overlapping trade
        # execution (main cycle vs TradingView webhook vs manual triggers);
        # a second guards overlapping main-cycle iterations (headless).
        self._trade_lock = asyncio.Lock()
        self._cycle_lock = asyncio.Lock()
        # TV dedupe registry: signal key -> first-seen epoch seconds.
        self._tv_seen: dict[str, float] = {}

        self._log.debug("orchestrator_created", config_path=config_path)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialize all subsystems in dependency order.

        1. ConfigManager
        2. HealthServer (no dependencies beyond config, starts early)
        3. DatabaseManager (connect + initialize + migrate)
        4. ExchangeAdapter (via factory)
        5. MarketDataEngine
        6. RiskManager
        7. ExecutionEngine
        8. Strategy system
        9. QuadBot (Telegram)
        10. MetricsCollector
        11. TradingView webhook

        If any step fails, previously-initialised subsystems are shut
        down before the exception propagates.
        """
        if self._started:
            self._log.warning("orchestrator_already_started")
            return

        self._log.info("orchestrator_starting")
        self._stop_event.clear()

        try:
            await self._init_config_manager()
            # Health server starts early — it has no deps beyond config,
            # so it's always reachable for liveness probes even when
            # database or exchange init is slow or failing.
            await self._init_health_server()
            await self._init_database()
            await self._init_exchange_adapter()

            # Sync position state from exchange on startup
            try:
                exchange_positions = await self._exchange_adapter.get_positions()
                self._log.info(
                    "on_start_positions_synced",
                    count=len(exchange_positions),
                )
            except Exception as exc:
                self._log.warning(
                    "on_start_positions_sync_failed",
                    error=str(exc),
                )

            # Configure futures account (leverage, margin mode, position mode)
            await self._setup_futures_account()

            await self._init_market_data()
            await self._init_risk_manager()
            await self._init_execution_engine()

            # Flatten any position left open by a previous run so rotation
            # starts with a clean slate and opens a fresh trade next cycle.
            try:
                await self._close_orphan_positions_on_start()
            except Exception as exc:
                self._log.warning(
                    "startup_positions_flatten_failed",
                    error=str(exc),
                )

            await self._init_strategies()
            await self._init_groq_ai()
            await self._init_optimizer()
            await self._init_telegram_bot()
            await self._init_metrics()
            await self._init_tradingview_webhook()

            self._started = True
            self._log.info(
                "orchestrator_started",
                mode=self._mode,
                strategies=list(self._active_strategies.keys()),
            )

        except Exception:
            self._log.exception("orchestrator_start_failed")
            await self._shutdown_all()
            raise

    async def stop(self) -> None:
        """Graceful shutdown in REVERSE dependency order.

        Safe to call multiple times (idempotent).  Each subsystem is
        given a short grace period before the orchestrator moves on.
        """
        if not self._started:
            return

        self._log.info("orchestrator_stopping")
        self._stop_event.set()
        await self._shutdown_all()
        self._started = False
        self._log.info("orchestrator_stopped")

    async def run_forever(self) -> None:
        """Start the orchestrator and run until a shutdown signal.

        Handles ``SIGTERM`` (Unix) and ``SIGINT`` (Ctrl+C) for graceful
        shutdown.  Creates a background task for the main trading cycle
        and waits for the stop event.
        """
        self._setup_signal_handlers()
        await self.start()

        self._log.info(
            "orchestrator_running",
            mode=self._mode,
            cycle_interval_s=self._cycle_interval,
        )

        # Create main cycle task
        cycle_task = asyncio.create_task(self._main_cycle())

        try:
            # Wait for stop signal
            await self._stop_event.wait()
        except (asyncio.CancelledError, KeyboardInterrupt):
            self._log.info("orchestrator_interrupted")
            self._stop_event.set()
        finally:
            # Cancel cycle task
            if not cycle_task.done():
                cycle_task.cancel()
                try:
                    await cycle_task
                except asyncio.CancelledError:
                    pass
            await self.stop()

    # ------------------------------------------------------------------
    # Signal handling
    # ------------------------------------------------------------------

    def _setup_signal_handlers(self) -> None:
        """Register signal handlers for graceful shutdown.

        On Unix, uses ``loop.add_signal_handler`` for both SIGTERM and
        SIGINT.  On Windows (where ``add_signal_handler`` is not
        supported), SIGINT is handled by asyncio's default behaviour
        (``KeyboardInterrupt`` -> ``CancelledError``).
        """

        def _on_sigterm() -> None:
            self._log.info("signal_received", signal="SIGTERM")
            self._stop_event.set()

        def _on_sigint() -> None:
            self._log.info("signal_received", signal="SIGINT")
            self._stop_event.set()

        loop = asyncio.get_running_loop()
        registered_any = False

        if sys.platform != "win32":
            try:
                loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
                registered_any = True
            except (NotImplementedError, RuntimeError):
                self._log.debug("sigterm_handler_not_available")

        try:
            loop.add_signal_handler(signal.SIGINT, _on_sigint)
            registered_any = True
        except (NotImplementedError, RuntimeError):
            self._log.debug("sigint_handler_not_available")

        if registered_any:
            self._log.debug("signal_handlers_registered")
        else:
            # Fallback for environments where add_signal_handler is
            # unavailable (e.g. Windows without ProactorEventLoop).
            # Ctrl+C will still trigger CancelledError via asyncio.run().
            self._log.debug("signal_handlers_fallback")

    # ------------------------------------------------------------------
    # Initialisation steps (private)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Graceful shutdown helper
    # ------------------------------------------------------------------

    async def _shutdown_all(self) -> None:
        """Shut down all subsystems in REVERSE dependency order.

        Each step is wrapped in try/except so that a failure in one
        subsystem does not prevent the remaining subsystems from
        shutting down.
        """
        self._log.info("shutting_down_all_subsystems")

        # 10. Metrics (no-op stop)
        # 9. Health server
        if self._health_server is not None:
            try:
                await self._health_server.stop()
            except Exception:
                self._log.exception("health_server_stop_error")
            self._health_server = None

        # 8. Telegram bot
        if self._bot is not None:
            try:
                await self._bot.stop()
            except Exception:
                self._log.exception("bot_stop_error")
            self._bot = None

        # 7. Strategies (no-op stop for now)
        self._active_strategies.clear()

        # 6. Execution engine
        if self._execution_engine is not None:
            try:
                await self._execution_engine.stop()
            except Exception:
                self._log.exception("execution_engine_stop_error")
            self._execution_engine = None

        # 5. Risk manager (no explicit stop method -- just clear state)
        self._risk_manager = None

        # 4. Market data engine
        if self._market_data is not None:
            try:
                await self._market_data.stop()
            except Exception:
                self._log.exception("market_data_stop_error")
            self._market_data = None

        # 3. Exchange adapter
        if self._exchange_adapter is not None:
            try:
                await self._exchange_adapter.disconnect()
            except Exception:
                self._log.exception("exchange_disconnect_error")
            self._exchange_adapter = None

        # 2a. Error log sink — must flush while the database is still open.
        if self._error_sink is not None:
            try:
                await self._error_sink.stop()
            except Exception:
                self._log.exception("error_sink_stop_error")
            self._error_sink = None

        # 2. Database manager
        if self._db_manager is not None:
            try:
                await self._db_manager.disconnect()
            except Exception:
                self._log.exception("database_disconnect_error")
            self._db_manager = None

        # 1b. Groq AI client (close HTTP session)
        if self._groq_client is not None:
            try:
                await self._groq_client.close()
            except Exception:
                self._log.exception("groq_client_close_error")
            self._groq_client = None

        # 1a. TradingView webhook (no explicit cleanup needed beyond health server)
        self._tv_webhook = None

        # 1. Config manager (no explicit cleanup)
        self._config_manager = None
        self._config_dict = {}

        self._log.info("all_subsystems_shut_down")

    # ------------------------------------------------------------------
    # Main trading cycle
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # AI-first trading cycle helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Telegram trade notifications
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Manual strategy execution (for Telegram /execute)
    # ------------------------------------------------------------------

    async def _build_strategy_context(self) -> StrategyContext | None:
        """Collect fresh market context for a single strategy evaluation.

        Fetches account state, positions, open orders, and option chains
        from the exchange adapter and market data engine, then assembles
        a ``StrategyContext`` for strategy evaluation.

        Returns
        -------
        StrategyContext or None
            ``None`` if exchange or market data is unavailable.
        """
        if self._exchange_adapter is None or self._market_data is None:
            return None

        try:
            account = await self._exchange_adapter.get_account()
            positions = await self._exchange_adapter.get_positions()
            open_orders = []
            try:
                open_orders = await self._exchange_adapter.get_open_orders()
            except Exception:  # noqa: S110  Non-critical; continue with empty orders
                pass

            return StrategyContext(
                account=account,
                positions=positions,
                orders=open_orders,
                config=self._config_dict,
            )
        except Exception as exc:
            self._log.exception("build_strategy_context_error", error=str(exc))
            return None

    async def execute_strategy(
        self, strategy_name: str, dry_run: bool = False
    ) -> dict[str, Any]:
        """Execute a single strategy by name and return the result.

        Parameters
        ----------
        strategy_name:
            The strategy name to execute (e.g., ``'cash_secured_put'``).
        dry_run:
            If True, evaluate but don't submit orders.

        Returns
        -------
        dict with keys: strategy, actions_count, actions (list), error (if any)
        """
        log = self._log.bind(strategy=strategy_name)
        log.info("execute_strategy_manual", dry_run=dry_run)

        try:
            # 1. Get the strategy instance
            strategy_cls = StrategyBase.registry.get(strategy_name)
            if strategy_cls is None:
                return {
                    "strategy": strategy_name,
                    "error": f"Unknown strategy: {strategy_name}",
                }

            strategy = strategy_cls()

            # 2. Collect market context
            context = await self._build_strategy_context()
            if context is None:
                return {
                    "strategy": strategy_name,
                    "error": "Failed to build market context",
                }

            # 3. Evaluate the strategy
            strategy_params = self._config_dict["strategy"].get(strategy_name)
            ctx = replace(context, strategy_params=strategy_params)
            actions = await strategy.evaluate(ctx)

            if not actions:
                return {"strategy": strategy_name, "actions_count": 0, "actions": []}

            # 4. Execute actions (or log if dry run)
            executed: list[dict[str, Any]] = []
            for action in actions:
                if action.type == "HOLD":
                    continue
                if not dry_run:
                    if self._execution_engine is None:
                        executed.append(
                            {
                                "action": action.type,
                                "error": "execution engine unavailable",
                            }
                        )
                        continue
                    try:
                        result = await self._execution_engine.execute(action, context)
                        executed.append(
                            {
                                "action": action.type,
                                "result": str(getattr(result, "status", "submitted")),
                            }
                        )
                    except Exception as exec_err:
                        executed.append({"action": action.type, "error": str(exec_err)})
                else:
                    executed.append({"action": action.type, "dry_run": True})

            return {
                "strategy": strategy_name,
                "actions_count": len(actions),
                "actions": [
                    {
                        "type": a.type,
                        "contract": a.contract,
                        "side": a.side,
                        "reason": a.reason,
                    }
                    for a in actions
                ],
                "executed": executed,
            }

        except Exception as exc:
            log.exception("execute_strategy_error", error=str(exc))
            return {"strategy": strategy_name, "error": str(exc)}

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Return full status from all subsystems.

        Returns
        -------
        dict
            Status dictionary with keys: orchestrator, config, exchange,
            market_data, risk, execution, strategies, telegram, health.
        """
        dry_run = self._is_dry_run
        result: dict[str, Any] = {
            "orchestrator": {
                "started": self._started,
                "mode": self._mode,
                "dry_run": dry_run,
                "cycle_interval_s": self._cycle_interval,
                "stop_event_set": self._stop_event.is_set(),
            },
            "config": {
                "loaded": self._config_manager is not None,
                "mode": self._mode,
                "dry_run": dry_run,
            },
            "exchange": {
                "connected": (
                    getattr(self._exchange_adapter, "is_connected", False)
                    if self._exchange_adapter
                    else False
                ),
                "testnet": (
                    bool(getattr(self._exchange_adapter, "is_testnet", False))
                    if self._exchange_adapter
                    else None
                ),
                "dry_run_guard_active": dry_run
                and not (
                    bool(getattr(self._exchange_adapter, "is_testnet", False))
                    if self._exchange_adapter
                    else False
                ),
            },
            "strategies": {
                "active_count": len(self._active_strategies),
                "active_names": list(self._active_strategies.keys()),
            },
            "telegram": {
                "enabled": self._bot is not None,
            },
            "ai": {
                "enabled": self._ai_enabled,
                "client_available": (
                    self._groq_client is not None and self._groq_client.is_available()
                ),
                "model": getattr(self._groq_client, "model", None)
                if self._groq_client
                else None,
                "cycle_count": self._ai_cycle_count,
                "cycle_interval_s": self._ai_cycle_interval,
                "last_cycle_time_ms": self._last_ai_cycle_time_ms,
                "last_action": self._last_ai_decision.get("action"),
                "last_error": self._last_ai_error,
                "consecutive_failures": self._consecutive_ai_failures,
                "requests_in_window": (
                    len(self._groq_client._request_timestamps)
                    if self._groq_client
                    else 0
                ),
            },
            "tradingview_webhook": {
                "enabled": self._tv_webhook is not None,
            },
        }

        # Market data status
        if self._market_data is not None:
            result["market_data"] = self._market_data.status()

        # Risk status
        if self._risk_manager is not None:
            try:
                result["risk"] = {
                    "trading_allowed": self._risk_manager.is_trading_allowed(),
                }
            except Exception:
                result["risk"] = {"error": "risk_status_unavailable"}

        # Execution stats
        if self._execution_engine is not None:
            try:
                result["execution"] = self._execution_engine.get_stats()
            except Exception:
                result["execution"] = {"error": "execution_stats_unavailable"}

        return result


# ============================================================================
# Internal helpers
# ============================================================================
