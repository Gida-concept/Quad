"""QuadOrchestrator — top-level application coordinator.

Wires all subsystems together and manages the full trading lifecycle:

    - Configuration loading (4-layer merge)
    - Database initialization and migrations
    - Exchange adapter creation (bybit)
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
import html as _html
import os
import signal
import sys
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

from quad.ai.context import MarketContext
from quad.config.manager import ConfigManager
from quad.config.schema import AiConfig, QuadConfig, TradingViewWebhookConfig
from quad.exchange.factory import create_exchange
from quad.execution.engine import ExecutionEngine
from quad.market_data.engine import MarketDataEngine
from quad.persistence.database import DatabaseManager
from quad.risk.manager import RiskManager
from quad.strategy.base import StrategyBase
from quad.strategy.factory import create_default_strategies
from quad.types.strategy import StrategyContext

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = "config/config.yaml"


# ============================================================================
# QuadOrchestrator
# ============================================================================


class QuadOrchestrator:
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
        Resolved trading mode: ``"binance"`` or ``"dry_run"``.
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

        # Telegram notification support
        self._telegram_bot: Any = None
        self._telegram_chat_id: int = 0

        # Indicator cache (key: "SYMBOL_TIMEFRAME", invalidated every 60s)
        self._indicator_cache: dict[str, dict[str, Any]] = {}

        # Lifecycle
        self._stop_event = asyncio.Event()
        self._started = False

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

    async def _init_config_manager(self) -> None:
        """Load configuration (ConfigManager)."""
        config_dir = Path(self._config_path).parent.resolve()
        self._config_manager = ConfigManager(config_dir=str(config_dir))
        self._config_dict = self._config_manager.to_dict()
        self._mode = self._config_manager.get_mode()
        self._cycle_interval = int(
            self._config_manager.get("trading.ai_cycle_interval")
        )

        # Merge Telegram env vars into config dict if not already present
        self._inject_env_overrides()

        self._log.info(
            "config_loaded",
            mode=self._mode,
            config_dir=str(config_dir),
        )

    def _inject_env_overrides(self) -> None:
        """Inject Telegram and operation env vars into the config dict.

        These env vars are not handled by ``ConfigManager``'s automatic
        env-var scanning (which only covers ``QUAD_*`` and ``BINANCE_*``
        prefixes), so we inject them manually.
        """
        # Telegram bot token
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if token and not _dot_get(self._config_dict, "telegram.bot_token"):
            self._set_telegram_config("bot_token", token)

        # Telegram notification chat ID
        chat_id_str = os.environ.get("TELEGRAM_NOTIFICATION_CHAT_ID")
        if chat_id_str:
            try:
                self._set_telegram_config("notification_chat_id", int(chat_id_str))
            except (ValueError, TypeError):
                self._log.warning(
                    "invalid_telegram_notification_chat_id",
                    value=chat_id_str,
                )

    def _set_telegram_config(self, key: str, value: Any) -> None:
        """Set a value in the ``telegram`` subsection of the config dict.

        Ensures the ``telegram`` key exists as a dict before assignment.

        Parameters
        ----------
        key:
            The config key to set (e.g. ``"bot_token"``).
        value:
            The value to store.
        """
        section = _dot_get(self._config_dict, "telegram", {})
        if not isinstance(section, dict):
            section = {}
        section[key] = value
        self._config_dict["telegram"] = section

    async def _init_database(self) -> None:
        """Initialise the database (connect + create tables + migrate)."""
        config_manager = self._config_manager
        if config_manager is None:
            raise RuntimeError("Config manager not initialized before database init")
        dsn = config_manager.get(
            "persistence.dsn",
            os.environ.get(
                "DATABASE_URL",
                self._config_dict["persistence"]["dsn"],
            ),
        )

        self._db_manager = DatabaseManager(
            dsn=str(dsn),
            min_pool_size=1,
            max_pool_size=5,
        )
        await self._db_manager.connect()
        await self._db_manager.initialize()
        await self._db_manager.migrate()
        self._log.info("database_initialized", dsn=self._db_manager.dsn)

    async def _init_exchange_adapter(self) -> None:
        """Create and connect the exchange adapter.

        Maps ``QUAD_MODE`` to the exchange implementation:
            - ``"dry_run"`` -> bybit with testnet=True
            - ``"bybit"`` -> configured exchange (testnet or live)
        """
        # Override exchange name based on mode
        mode = self._mode
        exchange_cfg: dict[str, Any] = dict(self._config_dict["exchange"])

        if mode == "dry_run":
            exchange_cfg["name"] = "bybit"
            exchange_cfg["testnet"] = True

        # Ensure rate_limit is a dict (for the exchange adapter)
        if not isinstance(exchange_cfg.get("rate_limit"), dict):
            exchange_cfg["rate_limit"] = {}

        # Create a combined config dict that the factory can read
        factory_config: dict[str, Any] = {}
        factory_config.update(self._config_dict)
        factory_config["exchange"] = exchange_cfg

        self._exchange_adapter = create_exchange(factory_config)
        await self._exchange_adapter.connect()
        self._log.info(
            "exchange_adapter_initialized",
            mode=mode,
            exchange_name=exchange_cfg["name"],
        )

    async def _setup_futures_account(self) -> None:
        """Configure futures account settings once at startup.

        Per configured symbol: sets leverage and margin mode.  Then syncs the
        position mode: if the exchange account is in HEDGE (dual-side) mode
        but the bot is configured for one-way, logs a LOUD warning (dual-side
        positions break single-sided SL/TP brackets and ``reduceOnly`` is
        forbidden in hedge mode) and only auto-switches to one-way on
        testnet/dry-run — never on live.

        Per-symbol failures are logged and swallowed so one bad symbol cannot
        abort startup.
        """
        trading_cfg = self._config_dict.get("trading", {})
        leverage = int(trading_cfg.get("leverage", 1))
        margin_mode = str(trading_cfg.get("margin_mode", "isolated"))
        position_mode = str(trading_cfg.get("position_mode", "one_way"))
        symbols = list(trading_cfg.get("underlyings", []))
        adapter = self._exchange_adapter
        if adapter is None:
            return

        for symbol in symbols:
            try:
                await adapter.set_leverage(symbol, leverage)
                self._log.info(
                    "account_setup_leverage_set",
                    symbol=symbol,
                    leverage=leverage,
                )
            except Exception as exc:
                self._log.warning(
                    "account_setup_leverage_failed",
                    symbol=symbol,
                    error=str(exc),
                )
            try:
                await adapter.set_margin_mode(symbol, margin_mode)
                self._log.info(
                    "account_setup_margin_mode_set",
                    symbol=symbol,
                    margin_mode=margin_mode,
                )
            except Exception as exc:
                if self._exchange_adapter.is_margin_mode_already_set(exc):
                    # Binance -4046 "No need to change margin type." — the
                    # symbol is already in the requested margin mode, so the
                    # call is a benign no-op.  Log at info, not a warning.
                    self._log.info(
                        "account_setup_margin_mode_already",
                        symbol=symbol,
                        margin_mode=margin_mode,
                    )
                else:
                    self._log.warning(
                        "account_setup_margin_mode_failed",
                        symbol=symbol,
                        error=str(exc),
                    )

        # Position mode sync
        try:
            current_mode = await adapter.get_position_mode()
        except Exception as exc:
            self._log.warning(
                "account_setup_position_mode_read_failed",
                error=str(exc),
            )
            return

        if current_mode == "hedge" and position_mode != "hedge":
            self._log.critical(
                "account_position_mode_conflict",
                current_mode=current_mode,
                configured_mode=position_mode,
                msg=(
                    "Exchange account is in HEDGE (dual-side) position mode "
                    "but the bot is configured for one-way.  Dual-side "
                    "positions break single-sided SL/TP brackets and "
                    "reduceOnly is forbidden in hedge mode.  Auto-switching "
                    "to one-way ONLY on testnet/dry-run; never on live."
                ),
            )
            adapter_is_testnet = bool(getattr(adapter, "is_testnet", False))
            if self._mode == "dry_run" or adapter_is_testnet:
                try:
                    await adapter.set_position_mode("one_way")
                    self._log.info(
                        "account_position_mode_switched",
                        to="one_way",
                        reason="dry_run_or_testnet",
                    )
                except Exception as exc:
                    self._log.warning(
                        "account_position_mode_switch_failed",
                        error=str(exc),
                    )
            else:
                self._log.critical(
                    "account_position_mode_live_conflict",
                    msg=(
                        "NOT auto-switching position mode on LIVE.  Manually "
                        "set the account to one-way (or change "
                        "trading.position_mode to hedge) before enabling "
                        "live trading."
                    ),
                )
        else:
            self._log.info(
                "account_position_mode_ok",
                current_mode=current_mode,
                configured_mode=position_mode,
            )

    @property
    def _is_dry_run(self) -> bool:
        """Whether dry-run mode is enabled.

        Either the top-level ``_dry_run`` config key (``QUAD_DRY_RUN``) is
        truthy, or the resolved mode is ``"dry_run"``.  This mirrors the
        execution engine's guard so status/metrics report the same effective
        state that actually blocks live orders.
        """
        if self._mode == "dry_run":
            return True
        val = self._config_dict.get("_dry_run", False)
        if isinstance(val, str):
            return val.lower() in ("1", "true", "yes")
        return bool(val)

    async def _init_market_data(self) -> None:
        """Initialise the market data engine."""
        self._market_data = MarketDataEngine(
            exchange_adapter=self._exchange_adapter,
            config=self._config_dict,
            db_manager=self._db_manager,
        )
        await self._market_data.start()
        self._log.info("market_data_engine_initialized")

    async def _init_risk_manager(self) -> None:
        """Initialise the risk management system."""
        self._risk_manager = RiskManager(
            config=self._config_dict,
            db_manager=self._db_manager,
        )
        self._log.info("risk_manager_initialized")

    async def _init_execution_engine(self) -> None:
        """Initialise the execution engine."""
        if self._risk_manager is None:
            raise RuntimeError("Risk manager not initialized before execution engine")
        self._execution_engine = ExecutionEngine(
            exchange_adapter=self._exchange_adapter,
            risk_manager=self._risk_manager,
            db_manager=self._db_manager,
            config=self._config_dict,
        )
        await self._execution_engine.start()
        self._log.info("execution_engine_initialized")

    async def _init_strategies(self) -> None:
        """Load and initialise all registered strategies."""
        self._active_strategies = create_default_strategies(self._config_dict)
        self._log.info(
            "strategies_initialized",
            count=len(self._active_strategies),
            names=list(self._active_strategies.keys()),
        )

    async def _init_optimizer(self) -> None:
        """Initialise the strategy self-optimizer (if dependencies are met).

        Requires:
        - ``_groq_client`` (initialised by ``_init_groq_ai``)
        - ``_db_manager`` (initialised by ``_init_database``)
        - ``retrain`` section in config
        """
        retrain_cfg = self._config_dict["retrain"]
        if not retrain_cfg.get("enabled"):
            self._log.info("optimizer_disabled_config")
            self._optimizer = None
            return

        if self._groq_client is None:
            self._log.info("optimizer_disabled_no_groq")
            self._optimizer = None
            return

        if self._db_manager is None:
            self._log.info("optimizer_disabled_no_db")
            self._optimizer = None
            return

        try:
            from quad.ai.optimizer import Optimizer
            from quad.persistence.repositories import (
                ConfigChangeRepository,
                DecisionRepository,
                OptimizationRecommendationRepository,
                OptimizationRunRepository,
                PerformanceSnapshotRepository,
                TradeRepository,
            )

            # Validate config as QuadConfig (Pydantic model needed by Optimizer)
            config = QuadConfig.model_validate(self._config_dict)

            db = self._db_manager

            self._optimizer = Optimizer(
                config=config,
                groq_client=self._groq_client,
                decision_repo=DecisionRepository(db),
                trade_repo=TradeRepository(db),
                performance_repo=PerformanceSnapshotRepository(db),
                run_repo=OptimizationRunRepository(db),
                recommendation_repo=OptimizationRecommendationRepository(db),
                config_change_repo=ConfigChangeRepository(db),
                config_dict=self._config_dict,
            )
            self._log.info("optimizer_initialized")

        except Exception as exc:
            self._log.exception("optimizer_init_failed", error=str(exc))
            self._optimizer = None

    async def _init_telegram_bot(self) -> None:
        """Initialise the Telegram bot (if enabled and configured).

        Failure to start the Telegram bot is non-fatal: the subsystem logs
        a warning and sets ``self._bot = None`` so the rest of the
        orchestrator continues running without the Telegram interface.
        """
        telegram_cfg = self._config_dict["telegram"]
        if not telegram_cfg.get("bot_token"):
            self._log.info("telegram_bot_disabled_no_token")
            self._bot = None
            return

        if not telegram_cfg.get("enabled"):
            self._log.info("telegram_bot_disabled_config")
            self._bot = None
            return

        # Lazy import to avoid PTB import errors when token is missing
        from quad.bot.bot import QuadBot

        try:
            self._bot = QuadBot(
                config=self._config_dict,
                orchestrator=self,
                risk_manager=self._risk_manager,
                execution_engine=self._execution_engine,
                market_data_engine=self._market_data,
                db_manager=self._db_manager,
                groq_client=self._groq_client,
                optimizer=self._optimizer,
            )
            await self._bot.start()

            # Capture the PTB Bot instance for trade notifications
            if self._bot is not None and self._bot.application is not None:
                self._telegram_bot = self._bot.application.bot
            self._telegram_chat_id = int(telegram_cfg.get("notification_chat_id") or 0)

            self._log.info(
                "telegram_bot_initialized",
                chat_id=self._telegram_chat_id,
            )
        except Exception as exc:
            self._log.warning(
                "telegram_bot_start_failed",
                error=str(exc),
                msg=(
                    "Telegram bot could not start. "
                    "The system will continue running without "
                    "the Telegram interface."
                ),
            )
            self._bot = None

    async def _init_health_server(self) -> None:
        """Initialise the health check HTTP server."""
        monitoring_cfg = self._config_dict["monitoring"]
        health_cfg = monitoring_cfg["health_server"]
        port = int(health_cfg.get("port", os.environ.get("QUAD_HEALTH_PORT")))
        enabled = health_cfg.get("enabled")

        if not enabled:
            self._log.info("health_server_disabled_config")
            self._health_server = None
            return

        from quad.monitoring.health import HealthServer

        self._health_server = HealthServer(
            port=port,
            config=self._config_dict,
            components=self._build_health_components(),
            metrics_collector=self._metrics,
        )
        await self._health_server.start()
        self._log.info("health_server_initialized", port=port)

    def _build_health_components(self) -> dict[str, Any]:
        """Build the component readiness dict for the health server."""
        components: dict[str, Any] = {
            "config": lambda: self._config_manager is not None,
            "database": lambda: self._db_manager is not None,
            "exchange": lambda: (
                self._exchange_adapter is not None
                and getattr(self._exchange_adapter, "is_connected", False)
            ),
            "market_data": lambda: (
                self._market_data is not None
                and self._market_data.status().get("uptime_seconds", 0) > 0
            ),
            "execution": lambda: self._execution_engine is not None,
            "strategies": lambda: len(self._active_strategies) > 0,
        }

        # Add Telegram if enabled
        if self._bot is not None:
            components["telegram_bot"] = lambda: getattr(self._bot, "is_running", False)

        # Add AI if enabled
        if self._groq_client is not None:
            components["groq_ai"] = lambda: self._groq_client is not None

        # Add TradingView webhook if enabled
        if self._tv_webhook is not None:
            components["tradingview_webhook"] = lambda: self._tv_webhook is not None

        return components

    async def _init_metrics(self) -> None:
        """Initialise the metrics collector and register instrumentation.

        The metrics collector is created before the health server so it
        can be passed to it.
        """
        from quad.monitoring.metrics import MetricsCollector

        self._metrics = MetricsCollector()
        self._metrics.set_gauge("orchestrator_started", 1.0)
        self._metrics.set_gauge("dry_run", 1.0 if self._is_dry_run else 0.0)
        self._log.info("metrics_collector_initialized")

    async def _init_groq_ai(self) -> None:
        """Initialise the Groq AI client (if API key is available)."""
        ai_cfg = AiConfig.model_validate(self._config_dict.get("ai"))
        api_key = os.environ.get("GROQ_API_KEY") or self._config_dict.get("ai", {}).get(
            "api_key"
        )

        if not api_key:
            self._log.info("groq_ai_disabled_no_key")
            self._groq_client = None
            self._ai_enabled = False
            return

        if not ai_cfg.enabled:
            self._log.info("groq_ai_disabled_config")
            self._groq_client = None
            self._ai_enabled = False
            return

        from quad.ai.groq import GroqClient

        self._groq_client = GroqClient(
            api_key=api_key,
            model=ai_cfg.model,
            timeout=ai_cfg.timeout,
            max_requests_per_day=ai_cfg.max_requests_per_day,
            config=self._config_dict,
        )

        # Set AI cycle interval from config, defaulting to 1 hour
        self._ai_cycle_interval = int(
            self._config_manager.get("trading.ai_cycle_interval")
            if self._config_manager
            else 3600
        )
        self._ai_enabled = True

        # Override main cycle interval for AI-first mode
        self._cycle_interval = self._ai_cycle_interval

        self._log.info(
            "groq_ai_initialized",
            model=self._groq_client.model,
            cycle_interval_s=self._ai_cycle_interval,
            max_requests_per_day=ai_cfg.max_requests_per_day,
        )

    async def _init_tradingview_webhook(self) -> None:
        """Initialise the TradingView webhook receiver.

        Registers a ``POST /webhook/tradingview`` route on the health
        server.  Requires ``tradingview_webhook.enabled`` in config.
        """
        tv_cfg = TradingViewWebhookConfig.model_validate(
            self._config_dict.get("tradingview_webhook", {})
        )
        if not tv_cfg.enabled:
            self._log.info("tradingview_webhook_disabled")
            self._tv_webhook = None
            return

        if self._health_server is None:
            self._log.warning(
                "tradingview_webhook_no_health_server",
            )
            self._tv_webhook = None
            return

        from quad.tradingview.signals import convert_to_action

        secret = tv_cfg.secret
        port = tv_cfg.port

        if not secret:
            self._log.warning(
                "tradingview_webhook_empty_secret",
                msg=(
                    "TradingView webhook is enabled but no secret is configured. "
                    "Set a non-empty secret via tradingview_webhook.secret in config "
                    "or the QUAD_TRADINGVIEW_WEBHOOK_SECRET env var. "
                    "The webhook will reject all requests until a secret is set."
                ),
            )

        # Check whether auth is strictly required (default: yes, fail closed)
        tv_allow_noauth = self._config_dict.get("tradingview_webhook", {}).get(
            "allow_without_secret", False
        )
        if not secret and not tv_allow_noauth:
            self._log.error(
                "tradingview_webhook_disabled_no_secret",
                msg=(
                    "TradingView webhook is enabled but no secret is configured and "
                    "allow_without_secret is not set.  The webhook is disabled.  "
                    "Either set a secret or explicitly set "
                    "tradingview_webhook.allow_without_secret: true in config."
                ),
            )
            self._tv_webhook = None
            return

        # Build the aiohttp handler
        async def _tv_webhook_handler(request: Any) -> Any:
            """Handle incoming TradingView webhook POST requests."""
            from aiohttp import web

            from quad.tradingview.parser import parse_alert

            log = self._log.bind()

            # Validate content type
            content_type = request.content_type or ""

            # Read body
            body = await request.read()
            raw_text = body.decode("utf-8", errors="replace")

            # Secret check (shared secret in payload)
            if secret:
                import json as _json

                try:
                    payload = (
                        _json.loads(raw_text)
                        if raw_text.strip().startswith("{")
                        else {}
                    )
                    if payload.get("secret") != secret:
                        log.warning("tv_webhook_invalid_secret")
                        return web.Response(status=403, text="Forbidden")
                except _json.JSONDecodeError:
                    log.warning("tv_webhook_invalid_json")
                    return web.Response(
                        status=400,
                        text="Invalid JSON payload",
                    )

            # Parse the alert
            parsed = parse_alert(body, content_type)
            signal = convert_to_action(parsed)

            if signal is None:
                log.warning(
                    "tv_webhook_unparseable",
                    body_preview=raw_text[:200],
                )
                return web.Response(
                    status=400,
                    text="Unparseable alert format",
                )

            log.info(
                "tv_webhook_received",
                symbol=signal.symbol,
                side=signal.side,
                quantity=str(signal.quantity),
                signal_type=signal.signal_type,
            )

            # Route to execution engine if available
            if self._execution_engine is not None and signal.signal_type != "exit":
                try:
                    from quad.types.risk import Action

                    action = Action(
                        type="ENTER",
                        strategy=f"tradingview_{signal.strategy_name}",
                        symbol=signal.symbol,
                        contract=signal.symbol,
                        side=signal.side,
                        # Preserve the exact signal quantity.  int() would
                        # truncate fractional quantities to 0, silently
                        # zeroing the order.
                        quantity=Decimal(str(signal.quantity)),
                        price=None,  # all orders are MARKET; no limit price
                        order_type="MARKET",
                        reason=f"TradingView alert: {signal.strategy_name}",
                        metadata=dict(signal.metadata or {}),
                    )
                    if self._execution_engine is None:
                        log.warning("tv_webhook_execution_engine_missing")
                        return web.Response(
                            status=503, text="Execution engine unavailable"
                        )
                    await self._execution_engine.execute(
                        action, StrategyContext(config=self._config_dict)
                    )
                except Exception as exc:
                    log.exception("tv_webhook_execution_error", error=str(exc))

            return web.json_response({"status": "ok"})

        # Register the route on the health server
        self._health_server.add_route(
            "POST", "/webhook/tradingview", _tv_webhook_handler
        )

        self._tv_webhook = {
            "enabled": True,
            "secret_configured": bool(secret),
            "port": port,
        }
        self._log.info(
            "tradingview_webhook_initialized",
            port=port,
            secret_configured=bool(secret),
        )

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

    async def _main_cycle(self) -> None:
        """Primary trading loop run as a background task.

        AI-Only Flow (24/7 forced trading mode):
        1. Force-close all open positions for a clean slate each cycle
        2. Collect full market context (candles, positions, account)
        3. Compute technical indicators from 150 fresh candles
        4. Build structured prompts for Groq LLM
        5. Call ``decide_trades()`` on the Groq client
        6. Parse the AI decision into an ``Action``
        7. Pass through risk manager
        8. Execute if risk checks pass (ENTER / EXIT)
        9. HOLD on AI failure — no deterministic fallback

        The cycle runs every ``ai_cycle_interval`` seconds (default 3600).
        Exactly one position at a time is enforced by the force-close step.
        """
        config_manager = self._config_manager
        if config_manager is None:
            self._log.warning("main_cycle_config_manager_missing")
            return
        underlyings = list(config_manager.get("trading", {}).get("underlyings", []))

        while not self._stop_event.is_set():
            cycle_start = time.monotonic()
            self._metrics_cycle_count += 1

            try:
                # ----------------------------------------------------------
                # 1. Account state
                # ----------------------------------------------------------
                account = await self._exchange_adapter.get_account()
                positions = await self._exchange_adapter.get_positions()
                open_orders = []
                try:
                    open_orders = await self._exchange_adapter.get_open_orders()
                except Exception:  # noqa: S110  Non-critical; continue with empty orders
                    pass

                # ----------------------------------------------------------
                # 1b. Resolve AI decision outcomes against live positions.
                #     Positions close on the exchange (TP/SL brackets); an
                #     ENTER decision stays outcome='open' until its symbol no
                #     longer has an open position.
                # ----------------------------------------------------------
                try:
                    await self._reconcile_decision_outcomes(positions)
                except Exception as exc:
                    self._log.warning(
                        "decision_outcome_reconcile_failed", error=str(exc)
                    )

                # ----------------------------------------------------------
                # 1c. Phase 3 prediction-quality metrics.  Lightweight: one
                #     indexed SELECT + in-memory arithmetic, gated by config.
                # ----------------------------------------------------------
                try:
                    await self._compute_ai_metrics()
                except Exception as exc:
                    self._log.warning("ai_metrics_failed", error=str(exc))

                # ----------------------------------------------------------
                # 2. Strategy context (futures-only; no option chains)
                # ----------------------------------------------------------
                context = StrategyContext(
                    account=account,
                    positions=positions,
                    orders=open_orders,
                    config=self._config_dict,
                )

                # ----------------------------------------------------------
                # 3. AI-First Decision (if enabled and available)
                # ----------------------------------------------------------
                ai_decision: dict[str, Any] = {}
                ai_used = False

                if self._ai_enabled and self._groq_client is not None:
                    try:
                        ai_available = self._groq_client.is_available()
                        if ai_available:
                            rotation_enabled = bool(
                                self._config_dict.get("ai", {})
                                .get("rotation", {})
                                .get("enabled", False)
                            )
                            if rotation_enabled:
                                ai_used = await self._run_ai_rotation(
                                    account, positions, open_orders, context
                                )
                            else:  # legacy path unchanged
                                if self._config_dict.get("trading", {}).get(
                                    "serial_trade_mode", True
                                ):
                                    closed = await self._close_all_positions()
                                    if closed:
                                        self._log.debug(
                                            "legacy_cycle_flattened",
                                        )
                                    else:
                                        self._log.warning(
                                            "legacy_cycle_flatten_incomplete",
                                        )
                                ai_decision = await self._run_ai_trading_cycle(
                                    underlyings, account, positions
                                )
                                ai_used = True
                                self._consecutive_ai_failures = 0
                                if ai_decision.get("action") in ("ENTER", "EXIT"):
                                    await self._execute_ai_action(ai_decision, context)
                        else:
                            self._log.warning("ai_not_available_skipping")
                    except Exception as exc:
                        self._consecutive_ai_failures += 1
                        self._last_ai_error = str(exc)
                        self._log.warning(
                            "ai_cycle_failed",
                            consecutive=self._consecutive_ai_failures,
                            error=str(exc),
                        )

                # ----------------------------------------------------------
                # 6. Update monitoring / metrics
                # ----------------------------------------------------------
                try:
                    risk_manager = self._risk_manager
                    if risk_manager is not None:
                        await risk_manager.update_monitoring(context)
                        # Check if any circuit breaker was triggered
                        cb_status = await risk_manager.get_status()
                        for cb_name, cb in cb_status.circuit_breakers.items():
                            if cb.active:
                                await self._notify_circuit_breaker(
                                    name=cb_name,
                                    reason=cb.reason or "Circuit breaker triggered",
                                    tier=getattr(cb, "tier", 0),
                                )
                except Exception as exc:
                    self._log.warning(
                        "risk_monitoring_update_error",
                        error=str(exc),
                    )

                # ----------------------------------------------------------
                # 6b. Periodic status / metrics (surface dry-run state)
                # ----------------------------------------------------------
                dry_run = self._is_dry_run
                testnet = bool(getattr(self._exchange_adapter, "is_testnet", False))
                self._log.info(
                    "cycle_status",
                    dry_run=dry_run,
                    testnet=testnet,
                    dry_run_guard_active=dry_run and not testnet,
                    mode=self._mode,
                    positions=len(positions),
                    ai_used=ai_used,
                )

                if self._metrics is not None:
                    self._metrics.set_gauge("active_positions", float(len(positions)))
                    self._metrics.set_gauge(
                        "active_strategies", float(len(self._active_strategies))
                    )
                    self._metrics.set_gauge("dry_run", 1.0 if dry_run else 0.0)
                    self._metrics.set_gauge(
                        "dry_run_guard_active",
                        1.0 if (dry_run and not testnet) else 0.0,
                    )
                    self._metrics.increment_counter("trading_cycles")

                    if ai_used:
                        self._metrics.increment_counter("ai_decisions")
                        self._metrics.set_gauge(
                            "ai_cycle_time_ms", self._last_ai_cycle_time_ms
                        )

                    if account is not None:
                        self._metrics.set_gauge(
                            "portfolio_value",
                            float(getattr(account, "total_usdt", Decimal(0))),
                        )

                # ----------------------------------------------------------
                # 7. Sleep for remaining interval
                # ----------------------------------------------------------
                elapsed = time.monotonic() - cycle_start
                sleep_time = max(0.0, float(self._cycle_interval) - elapsed)

                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)

            except asyncio.CancelledError:
                self._log.info("main_cycle_cancelled")
                break
            except Exception as exc:
                self._log.exception(
                    "main_cycle_error",
                    error=str(exc),
                )
                # On unexpected error, wait the full interval before retrying
                await asyncio.sleep(float(self._cycle_interval))

    # ------------------------------------------------------------------
    # AI-first trading cycle helpers
    # ------------------------------------------------------------------

    async def _run_ai_trading_cycle(
        self,
        underlyings: list[str],
        account: Any,
        positions: Any,
    ) -> dict[str, Any]:
        """Run the AI trading decision cycle.

        1. Collect market context (candles, account, positions, chains)
        2. Compute technical indicators
        3. Build structured prompts
        4. Call ``decide_trades()``
        5. Log and return the decision

        Parameters
        ----------
        underlyings:
            List of trading pairs to analyse.
        account:
            Current account state from the exchange adapter.
        positions:
            Current open positions from the exchange adapter.

        Returns
        -------
        dict
            The parsed trading decision from the LLM, or a HOLD dict on failure.
        """
        ai_start = time.monotonic()
        self._ai_cycle_count += 1

        try:
            # 1. Collect market context
            from quad.ai.context import collect_market_context

            context = await collect_market_context(
                exchange_adapter=self._exchange_adapter,
                market_data_engine=self._market_data,
                db_manager=self._db_manager,
                config=self._config_dict,
            )
            self._log.debug(
                "market_context_collected",
                pairs=len(context.candles),
                positions=len(context.positions),
                errors=len(context.errors),
            )

            # 2. Compute technical indicators per pair/timeframe (cache disabled)
            from quad.ai.ta import compute_indicators

            indicators: dict[str, dict[str, Any]] = {}
            for key, candles in context.candles.items():
                try:
                    indicators[key] = compute_indicators(candles)
                except Exception as exc:
                    self._log.warning(
                        "indicator_computation_failed",
                        key=key,
                        error=str(exc),
                    )
                    indicators[key] = {}

            # 3. Build structured prompts
            from quad.ai.prompt import build_trading_prompt

            prompts = build_trading_prompt(
                context=context,
                indicators=indicators,
                config=self._config_dict,
            )

            # 4. Call Groq for trading decision
            self._log.debug(
                "ai_decision_request",
                cycle=self._ai_cycle_count,
                system_prompt_len=len(prompts["system"]),
                user_prompt_len=len(prompts["user"]),
            )

            ai_trade_cfg = self._config_dict.get("ai") or {}
            decision = await self._groq_client.decide_trades(
                system_prompt=prompts["system"],
                user_prompt=prompts["user"],
                temperature=ai_trade_cfg.get("temperature"),
                max_tokens=ai_trade_cfg.get("max_tokens"),
            )

            # Track timing
            self._last_ai_cycle_time_ms = round((time.monotonic() - ai_start) * 1000, 2)
            self._last_ai_decision = decision

            # 5. Log decision to database
            try:
                await self._log_ai_decision(decision, context)
            except Exception as exc:
                self._log.warning("ai_decision_log_failed", error=str(exc))

            self._log.debug(
                "ai_decision_received",
                action=decision.get("action", "unknown"),
                strategy=decision.get("strategy"),
                confidence=decision.get("confidence"),
                cycle_time_ms=self._last_ai_cycle_time_ms,
            )

            return decision

        except Exception as exc:
            self._log.warning(
                "ai_trading_cycle_crashed",
                error=str(exc),
                cycle=self._ai_cycle_count,
            )
            return {
                "reasoning": f"Trading cycle exception: {exc}",
                "action": "HOLD",
                "confidence": 0.0,
                "indicators": {},
            }

    async def _run_ai_rotation(
        self,
        account: Any,
        positions: Any,
        open_orders: Any,
        context: StrategyContext,
    ) -> bool:
        """Run the pair-rotation AI cycle (one pair at a time).

        CASE A — a position is open: scan ONLY that pair and manage it
        (EXIT / adjust_stop / reduce_position); never open a second.

        CASE B — flat: scan each configured pair once, starting at the
        rotation index, sleeping ``retry_sleep`` seconds between attempts.
        Advance to the next pair only when the current position closes.

        Returns
        -------
        bool
            ``True`` if the rotation made progress this cycle, ``False``
            if it could not run (no pairs / Groq unavailable).
        """
        pairs = list(self._config_dict.get("ai", {}).get("pairs", []))
        if (
            not pairs
            or self._groq_client is None
            or not self._groq_client.is_available()
        ):
            self._log.warning("ai_rotation_unavailable")
            return False
        retry_sleep = float(
            self._config_dict["ai"]["rotation"].get("retry_sleep_seconds", 30.0)
        )
        from quad.types.domain import PositionStatus

        open_positions = [
            p for p in positions if getattr(p, "status", None) == PositionStatus.OPEN
        ]

        # CASE A — a position is open: close it and open a fresh trade
        # (1 trade per cycle) unless close_open_position_each_cycle is off,
        # in which case the previous hold-until-TP/SL behavior applies.
        if open_positions:
            held = open_positions[0]
            held_symbol = getattr(held, "symbol", "") or getattr(
                held, "contract_symbol", ""
            )
            self._current_symbol = held_symbol
            self._log.info("rotation_managing_open_position", symbol=held_symbol)

            rotation_cfg = self._config_dict.get("ai", {}).get("rotation", {}) or {}
            close_each_cycle = bool(
                rotation_cfg.get("close_open_position_each_cycle", True)
            )

            if close_each_cycle:
                # Roll the position every hour: close the current trade (the
                # EXIT is broadcast to Telegram by _notify_trade below) and
                # fall through to CASE B to scan for a fresh entry.  Never
                # open a second position in the same cycle: CASE B opens at
                # most one ENTER.
                self._log.info(
                    "rotation_cycle_rolling_position",
                    symbol=held_symbol,
                )
                closed = await self._close_all_positions()
                self._rotation_hold_since.pop(held_symbol, None)
                if closed:
                    self._log.info(
                        "rotation_cycle_closed",
                        symbol=held_symbol,
                    )
                    # Only announce the EXIT after the close is confirmed
                    # flat -- a trade that is still open on Binance must not
                    # be broadcast as closed.
                    try:
                        from decimal import Decimal as _D

                        closed_pnl = self._compute_position_pnl(
                            entry_price=_D(str(getattr(held, "entry_price", 0) or 0)),
                            exit_price=_D(
                                str(getattr(held, "current_price", 0) or 0)
                            ),
                            quantity=_D(str(getattr(held, "quantity", 0) or 0)),
                            side=str(getattr(held, "side", "")),
                        )
                        pnl_text = self._format_pnl(
                            closed_pnl,
                            _D(str(getattr(held, "entry_price", 0) or 0)),
                        )
                        await self._notify_trade(
                            action_type="EXIT",
                            strategy="rotation_roll",
                            contract=held_symbol,
                            side=self._side_label(getattr(held, "side", "")),
                            quantity=str(getattr(held, "quantity", "")),
                            price=None,
                            reason=(
                                "Rotation cycle: closing previous trade "
                                "before opening a new one"
                            ),
                            pnl=pnl_text,
                        )
                    except Exception as exc:
                        self._log.warning(
                            "rotation_exit_notify_failed",
                            symbol=held_symbol,
                            error=str(exc),
                        )
                    # Advance the rotation index to the pair AFTER the closed
                    # position (ADR-080: "advance to the next pair when the
                    # position closes").  Deterministic regardless of the
                    # prior index state: closing BTC -> next scan is ETH,
                    # not BTC again and not a skipped pair.
                    try:
                        held_idx = pairs.index(held_symbol)
                    except ValueError:
                        held_idx = self._rotation_index
                    self._rotation_index = (held_idx + 1) % len(pairs)
                    # Refresh the in-memory position list (shared with the
                    # caller) so cycle_status / metrics show the post-close
                    # state instead of the stale pre-close position.
                    try:
                        fresh_positions = await self._exchange_adapter.get_positions()
                        if isinstance(fresh_positions, list) and fresh_positions:
                            positions[:] = [
                                p
                                for p in fresh_positions
                                if getattr(p, "status", None) != PositionStatus.OPEN
                            ]
                        elif isinstance(fresh_positions, list):
                            positions[:] = fresh_positions
                    except Exception as exc:
                        self._log.warning(
                            "rotation_positions_refresh_failed",
                            symbol=held_symbol,
                            error=str(exc),
                        )
                else:
                    self._log.warning(
                        "rotation_cycle_close_failed",
                        symbol=held_symbol,
                    )
                    return True  # do not open a second trade when close failed
            else:
                # Legacy behavior: hold until TP/SL bracket triggers.
                # Stale-position guard: force-close a position held longer than
                # ai.rotation.max_hold_seconds so a trade can't hang for hours
                # waiting for a TP/SL bracket that never triggers.
                max_hold_s = float(rotation_cfg.get("max_hold_seconds", 0.0))
                if max_hold_s > 0:
                    held_since = self._rotation_hold_since.get(held_symbol)
                    now = time.monotonic()
                    if held_since is None:
                        self._rotation_hold_since[held_symbol] = now
                    elif now - held_since >= max_hold_s:
                        self._log.info(
                            "rotation_max_hold_reached",
                            symbol=held_symbol,
                            max_hold_seconds=max_hold_s,
                        )
                        closed = await self._close_all_positions()
                        self._rotation_hold_since.pop(held_symbol, None)
                        if closed:
                            self._log.info(
                                "rotation_max_hold_closed",
                                symbol=held_symbol,
                            )
                            return True  # flat now; next cycle opens a fresh trade
                        self._log.warning(
                            "rotation_max_hold_close_failed",
                            symbol=held_symbol,
                        )

                # Price-bracket guard: if the mark price is clearly beyond a
                # TP/SL trigger but the bracket order has not fired, the position
                # would hang indefinitely - force-close it now.
                try:
                    violated, which = await self._price_bracket_violation(
                        held_symbol,
                        getattr(held, "side", None),
                        open_orders,
                    )
                except Exception as exc:
                    self._log.warning(
                        "price_bracket_check_failed",
                        symbol=held_symbol,
                        error=str(exc),
                    )
                    violated, which = False, ""
                if violated:
                    self._log.info(
                        "rotation_price_beyond_bracket",
                        symbol=held_symbol,
                        bracket=which,
                    )
                    closed = await self._close_all_positions()
                    self._rotation_hold_since.pop(held_symbol, None)
                    if closed:
                        self._log.info(
                            "rotation_price_bracket_closed",
                            symbol=held_symbol,
                            bracket=which,
                        )
                        return True  # flat now; next cycle opens a fresh trade
                    self._log.warning(
                        "rotation_price_bracket_close_failed",
                        symbol=held_symbol,
                        bracket=which,
                    )

                try:
                    decision = await self._scan_pair(
                        held_symbol
                    )  # raises on Groq/API error
                except Exception as exc:  # CancelledError NOT caught (BaseException)
                    self._log.warning("ai_scan_error", symbol=held_symbol, error=str(exc))
                    return False
                hold_action = decision.get("action", "HOLD")
                if hold_action in ("ENTER", "EXIT"):
                    # Hold-until-TP/SL: while a position is open, never open a new
                    # trade and never close early.  The position is closed ONLY by
                    # the STOP_LOSS / TAKE_PROFIT bracket orders attached at entry.
                    self._log.info(
                        "rotation_hold_until_tp_sl",
                        action=hold_action,
                        symbol=held_symbol,
                        reason="position open; close only via TP/SL bracket",
                    )
                elif hold_action in ("adjust_stop", "reduce_position"):
                    await self._execute_ai_action(decision, context)
                return True  # HOLD / no-op -> keep holding; wait for next hour

        # CASE B — flat: scan each pair once, starting at the rotation index.
        self._rotation_hold_since.clear()  # flat: nothing held to track
        self._rotation_index %= len(pairs)
        scanned = 0
        while scanned < len(pairs):
            if not self._groq_client.is_available():  # daily limit hit mid-scan
                self._log.warning("ai_rate_limit_hit_stopping_scan")
                break
            symbol = pairs[self._rotation_index % len(pairs)]
            self._current_symbol = symbol
            self._log.info(
                "rotation_scanning_pair", symbol=symbol, index=self._rotation_index
            )
            try:
                decision = await self._scan_pair(symbol)
            except Exception as exc:
                self._consecutive_ai_failures += 1
                self._last_ai_error = str(exc)
                self._log.warning("ai_scan_failed", symbol=symbol, error=str(exc))
                if isinstance(exc, RuntimeError) and "rate limit" in str(exc).lower():
                    break  # stop burning requests this hour
                decision = {
                    "action": "HOLD",
                    "reasoning": f"scan exception: {exc}",
                    "confidence": 0.0,
                }

            action = decision.get("action", "HOLD")
            if action == "ENTER":
                if await self._execute_ai_action(decision, context):
                    # Position opened -> index now points at the NEXT pair, so the
                    # post-close scan continues after this one.
                    self._rotation_index = (self._rotation_index + 1) % len(pairs)
                    self._log.info("rotation_opened_position", symbol=symbol)
                    return True
                self._log.warning(
                    "rotation_enter_failed_advancing", symbol=symbol
                )  # risk/exec rejected
            elif action == "EXIT":
                self._log.info(
                    "rotation_exit_without_position_advancing", symbol=symbol
                )

            self._rotation_index = (self._rotation_index + 1) % len(pairs)
            scanned += 1
            if scanned < len(pairs):
                await asyncio.sleep(retry_sleep)  # 30s between HOLD scans

        self._log.info("rotation_scan_complete_all_hold", scanned=scanned)
        return True

    def _position_side_for_symbol(
        self,
        context: StrategyContext | MarketContext,
        symbol: str,
    ) -> Any:
        """Return the open position side for ``symbol``, or ``None`` when flat.

        Used by the validator and the execution backstop to derive EXIT sides
        deterministically and to decide whether an EXIT is even possible.

        Parameters
        ----------
        context:
            Current strategy context (contains live positions).
        symbol:
            Contract symbol, e.g. ``"BTCUSDT"``.

        Returns
        -------
        Any
            The ``PositionSide`` of the open position for ``symbol``, or
            ``None`` when no such position is open.
        """
        from quad.types.domain import PositionStatus

        for p in context.positions:
            sym = getattr(p, "symbol", "") or getattr(p, "contract_symbol", "")
            if sym == symbol and getattr(p, "status", None) == PositionStatus.OPEN:
                return getattr(p, "side", None)
        return None

    @staticmethod
    def _merge_indicators_for_symbol(
        indicators: dict[str, dict[str, Any]],
        symbol: str,
    ) -> dict[str, Any]:
        """Merge per-timeframe indicator dicts for ``symbol`` into one dict.

        ``indicators`` keys look like ``"BTCUSDT_15m"``.  The merged dict is
        passed to the validator's plausibility gate; later timeframes (e.g.
        1h) override earlier ones, biasing the gate toward the macro view.

        Parameters
        ----------
        indicators:
            Dict of ``{pair_timeframe_key: indicator_dict}``.
        symbol:
            Contract symbol to filter on, e.g. ``"BTCUSDT"``.

        Returns
        -------
        dict[str, Any]
            Merged indicator dict for ``symbol`` (possibly empty).
        """
        merged: dict[str, Any] = {}
        for key, ind in indicators.items():
            if key.split("_", 1)[0] == symbol:
                merged.update(ind or {})
        return merged

    async def _scan_pair(self, symbol: str) -> dict[str, Any]:
        """Run the single-pair AI pipeline for ``symbol``.

        Mirrors ``_run_ai_trading_cycle`` but scoped to one pair: collects
        market context with ``ai.pairs=[symbol]``, computes indicators,
        builds prompts, calls Groq, logs the decision, and pins the
        decision contract to ``symbol`` so the LLM cannot hallucinate a
        different pair.

        Parameters
        ----------
        symbol:
            Trading pair to scan, e.g. ``"BTCUSDT"``.

        Returns
        -------
        dict
            The parsed trading decision from the LLM.

        Raises
        ------
        Exception
            Groq / API errors propagate to the caller (``_run_ai_rotation``).
        """
        ai_start = time.monotonic()
        self._ai_cycle_count += 1
        cfg = dict(self._config_dict)
        cfg["ai"] = dict(self._config_dict["ai"])
        cfg["ai"]["pairs"] = [symbol]

        from quad.ai.context import collect_market_context

        context = await collect_market_context(
            exchange_adapter=self._exchange_adapter,
            market_data_engine=self._market_data,
            db_manager=self._db_manager,
            config=cfg,
        )

        from quad.ai.ta import compute_indicators

        indicators: dict[str, dict[str, Any]] = {}
        for key, candles in context.candles.items():
            try:
                indicators[key] = compute_indicators(candles)
            except Exception as exc:
                self._log.warning(
                    "indicator_computation_failed",
                    key=key,
                    error=str(exc),
                )
                indicators[key] = {}

        # Merge per-timeframe indicators for this symbol and resolve the
        # open position side — both needed before the local signal build.
        indicator_snapshot = self._merge_indicators_for_symbol(indicators, symbol)
        position_side = self._position_side_for_symbol(context, symbol)

        from quad.ai.ta import generate_local_signal

        # Extract funding rate for the symbol from the market context.
        funding_rate = None
        funding_annual_pct = None
        if context.funding_rates:
            fr = context.funding_rates.get(symbol)
            if fr is not None:
                funding_rate = float(getattr(fr, "funding_rate", 0) or 0)
                # Annualize: funding_rate * 3 per-day * 365 days * 100 = %
                funding_annual_pct = round(funding_rate * 3 * 365 * 100, 2)

        # Build a compact local signal from the computed indicators.
        # The AI receives this distilled signal (not raw candles) and makes
        # only the final ENTER/HOLD/EXIT decision.
        local_signal = generate_local_signal(
            indicator_snapshot,
            symbol=symbol,
            funding_rate=funding_rate,
            funding_annual_pct=funding_annual_pct,
        )

        from quad.ai.prompt import build_final_judgement_prompt

        prompts = build_final_judgement_prompt(
            local_signal=local_signal,
            positions=context.positions,
            account=context.account,
            config=cfg,
        )
        decision = await self._groq_client.decide_trades(  # may raise (rate-limit/API)
            system_prompt=prompts["system"],
            user_prompt=prompts["user"],
            temperature=cfg["ai"].get("temperature"),
            max_tokens=cfg["ai"].get("max_tokens"),
        )

        self._last_ai_cycle_time_ms = round((time.monotonic() - ai_start) * 1000, 2)

        # ----------------------------------------------------------------
        # Phase 1 inversion guard: deterministically validate the decision.
        # The LLM forecasts a DIRECTION; normalize_decision derives the order
        # side and (in veto mode) rejects implausible entries.  A rejected
        # decision is replaced with a safe HOLD so it never reaches execution.
        # ----------------------------------------------------------------
        from quad.ai.validator import normalize_decision

        validator_cfg = cfg.get("ai", {}).get("validator", {})
        gate_mode = validator_cfg.get("gate_mode", "warn")
        min_confidence_to_trade = validator_cfg.get("min_confidence_to_trade", 0.0)

        result = normalize_decision(
            decision,
            position_side=position_side,
            indicators=indicator_snapshot,
            gate_mode=gate_mode,
            min_confidence_to_trade=min_confidence_to_trade,
        )
        decision = result.decision
        decision["indicators"] = indicator_snapshot

        if not result.ok:
            self._log.warning(
                "ai_decision_rejected",
                action=decision.get("action"),
                contract=symbol,
                reason=result.rejected_reason,
                corrected=result.corrected,
            )
            # Replace with a safe HOLD (same style as contract-pinning):
            # a rejected decision must never reach execution.
            decision = {
                "reasoning": f"Decision rejected by validator: {result.rejected_reason}",
                "action": "HOLD",
                "direction": "NEUTRAL",
                "side": None,
                "contract": symbol,
                "quantity": None,
                "confidence": 0.0,
                "gate_result": decision.get("gate_result", "not_checked"),
                "indicators": indicator_snapshot,
            }
        elif result.corrected:
            self._log.info(
                "ai_decision_corrected",
                action=decision.get("action"),
                contract=symbol,
                corrected=result.corrected,
                side=decision.get("side"),
            )

        self._last_ai_decision = decision
        try:
            await self._log_ai_decision(decision, context)
        except Exception as exc:
            self._log.warning("ai_decision_log_failed", error=str(exc))

        # Pin contract: the prompt contains ONLY this pair, so any other contract is a hallucination.
        if decision.get("action") in (
            "ENTER",
            "EXIT",
            "adjust_stop",
            "reduce_position",
        ):
            if decision.get("contract") != symbol:
                self._log.warning(
                    "ai_contract_pinned", expected=symbol, got=decision.get("contract")
                )
            decision["contract"] = symbol
        return decision

    async def _price_bracket_violation(
        self,
        symbol: str,
        position_side: Any,
        open_orders: Any,
    ) -> tuple[bool, str]:
        """Check whether the live mark price is clearly beyond a bracket.

        Compares the current mark price against the STOP_MARKET (stop-loss)
        and TAKE_PROFIT_MARKET triggers among *open_orders* for ``symbol``.
        A LONG position is violated when mark <= SL - tolerance or
        mark >= TP + tolerance; a SHORT position is the mirror image.

        Returns
        -------
        tuple[bool, str]
            ``(True, "sl" | "tp")`` when the price is clearly beyond a
            trigger but the bracket has not fired; ``(False, "")`` otherwise.
        """
        rotation_cfg = self._config_dict.get("ai", {}).get("rotation", {})
        if not rotation_cfg.get("price_bracket_check", True):
            return False, ""
        tolerance_pct = float(
            rotation_cfg.get("price_bracket_tolerance_pct", 0.5)
        )
        tolerance = Decimal(str(tolerance_pct)) / Decimal(100)

        sl_trigger: Decimal | None = None
        tp_trigger: Decimal | None = None
        for order in open_orders or []:
            if getattr(order, "symbol", "") != symbol:
                continue
            otype = str(getattr(order, "order_type", "")).upper()
            stop_price = getattr(order, "stop_price", None)
            if stop_price is None:
                continue
            if otype in ("STOP_MARKET", "STOP_LOSS", "STOP"):
                sl_trigger = Decimal(str(stop_price))
            elif otype in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT"):
                tp_trigger = Decimal(str(stop_price))

        if sl_trigger is None and tp_trigger is None:
            return False, ""

        try:
            mark = await self._exchange_adapter.get_mark_price(symbol)
        except Exception as exc:
            self._log.warning(
                "price_bracket_check_mark_unavailable",
                symbol=symbol,
                error=str(exc),
            )
            return False, ""
        if mark is None or mark <= 0:
            return False, ""

        from quad.types.domain import PositionSide

        is_long = position_side == PositionSide.LONG
        if sl_trigger is not None:
            if is_long and mark <= sl_trigger * (1 - tolerance):
                return True, "sl"
            if not is_long and mark >= sl_trigger * (1 + tolerance):
                return True, "sl"
        if tp_trigger is not None:
            if is_long and mark >= tp_trigger * (1 + tolerance):
                return True, "tp"
            if not is_long and mark <= tp_trigger * (1 - tolerance):
                return True, "tp"
        return False, ""

    async def _close_orphan_positions_on_start(self) -> None:
        """Flatten positions left open by a previous run at startup.

        Gated by ``ai.rotation.enabled`` and
        ``ai.rotation.close_positions_on_start`` (both default on for the
        rotation mode).  When enabled and open positions exist, cancels the
        old TP/SL brackets and market-closes the positions so the first
        rotation cycle scans flat and can open a fresh trade instead of
        holding a stale one for hours.
        """
        ai_cfg = self._config_dict.get("ai", {})
        rotation_cfg = ai_cfg.get("rotation", {})
        if not rotation_cfg.get("enabled", False):
            self._log.debug("startup_rotation_disabled_skip_flatten")
            return
        if not rotation_cfg.get("close_positions_on_start", True):
            self._log.debug("startup_flatten_disabled_by_config")
            return

        open_positions = await self._exchange_adapter.get_positions()
        from quad.types.domain import PositionStatus

        open_positions = [
            p
            for p in open_positions
            if getattr(p, "status", None) == PositionStatus.OPEN
        ]
        if not open_positions:
            self._log.info("startup_no_orphan_positions")
            return

        self._log.info(
            "startup_flattening_previous_positions",
            count=len(open_positions),
            symbols=[getattr(p, "symbol", "") for p in open_positions],
        )
        closed = await self._close_all_positions()
        self._log.info(
            "startup_positions_flattened" if closed else "startup_positions_flatten_incomplete",
            requested=len(open_positions),
        )

    async def _close_all_positions(self) -> bool:
        """Close all open positions using MARKET EXIT orders.

        Called when ``serial_trade_mode`` is enabled and a new ENTER
        action is about to be executed.  The method:

        1. Fetches all open positions from the exchange adapter.
        2. Cancels any open orders on those positions.
        3. Builds an EXIT ``Action`` for each position with MARKET order type.
        4. Submits each EXIT through the execution engine.
        5. Logs the results with structlog.

        Returns
        -------
        bool
            ``True`` if all positions were closed successfully,
            ``False`` if any position failed to close or no positions
            were found.
        """
        log = self._log.bind()
        if self._execution_engine is None:
            log.warning("close_all_positions_no_execution_engine")
            return False

        # 1. Get all open positions
        try:
            positions = await self._exchange_adapter.get_positions()
        except Exception as exc:
            log.exception("close_all_positions_fetch_error", error=str(exc))
            return False

        # Filter to only OPEN positions
        from quad.types.domain import PositionStatus

        open_positions = [
            p for p in positions if getattr(p, "status", None) == PositionStatus.OPEN
        ]

        if not open_positions:
            log.debug("close_all_positions_no_open_positions")
            return True  # Nothing to close — success by definition

        log.debug(
            "close_all_positions_started",
            count=len(open_positions),
        )

        # 2. Cancel all open orders
        try:
            open_orders = await self._exchange_adapter.get_open_orders()
            for order in open_orders:
                try:
                    await self._exchange_adapter.cancel_order(
                        order.id, getattr(order, "symbol", "")
                    )
                    log.info(
                        "close_all_positions_order_cancelled",
                        order_id=order.id,
                        symbol=getattr(order, "symbol", "unknown"),
                    )
                except Exception as exc:
                    log.warning(
                        "close_all_positions_cancel_order_error",
                        order_id=getattr(order, "id", "unknown"),
                        error=str(exc),
                    )
        except Exception as exc:
            log.warning(
                "close_all_positions_fetch_orders_error",
                error=str(exc),
            )
            # Continue — non-critical

        # 3. Build and execute EXIT actions
        from quad.types.risk import Action

        close_tasks: list[asyncio.Task] = []
        for position in open_positions:
            # Determine close side: LONG -> SELL, SHORT -> BUY
            pos_side = getattr(position, "side", None)
            if pos_side is None:
                log.warning(
                    "close_all_positions_unknown_side",
                    contract=getattr(
                        position, "symbol", getattr(position, "contract_symbol", "")
                    ),
                )
                continue

            from quad.types.domain import PositionSide as PS

            close_side = "SELL" if pos_side == PS.LONG else "BUY"

            action = Action(
                type="EXIT",
                strategy="serial_close",
                contract=getattr(
                    position, "symbol", getattr(position, "contract_symbol", "")
                ),
                side=close_side,
                # Preserve the exact fractional quantity.  int() would
                # truncate fractional quantities to 0, silently zeroing the
                # order (and the engine now rejects zero/negative quantities).
                quantity=Decimal(str(getattr(position, "quantity", 0))),
                order_type="MARKET",
                price=None,
                reason="Serial trade mode: closing position before new ENTER",
                metadata={
                    "serial_close": True,
                    # Entry price at close time so the engine can persist the
                    # realized PnL for this trade.
                    "entry_price": str(getattr(position, "entry_price", 0) or 0),
                    # Position side (LONG/SHORT), NOT the closing trade side:
                    # the PnL formula must know the held direction.
                    "position_side": str(
                        getattr(position, "side", "") or ""
                    ),
                },
            )

            # 4. Execute through execution engine
            # Track the position alongside the task so per-position outcomes
            # (and realized PnL, when derivable) can be reported accurately.
            task = asyncio.create_task(
                self._execution_engine.execute(
                    action, StrategyContext(config=self._config_dict)
                )
            )
            close_tasks.append((position, action, task))

        # 5. Wait for all close orders to complete
        results: list[tuple[Any, Action, OrderResult | Exception | None]] = []
        for position, action, task in close_tasks:
            outcome = await task
            results.append((position, action, outcome))

        success_count = 0
        fail_count = 0
        closed_details: list[dict[str, Any]] = []
        for position, action, result in results:
            symbol = getattr(position, "symbol", "") or getattr(
                position, "contract_symbol", ""
            )
            if isinstance(result, Exception):
                fail_count += 1
                log.exception(
                    "close_all_positions_execution_error",
                    symbol=symbol,
                    error=str(result),
                )
                continue
            if result is None or result.status not in (
                "FILLED",
                "NEW",
                "PARTIALLY_FILLED",
            ):
                fail_count += 1
                log.warning(
                    "close_all_positions_not_confirmed",
                    symbol=symbol,
                    status=getattr(result, "status", "unknown"),
                )
                continue
            success_count += 1
            closed_details.append(
                {
                    "symbol": symbol,
                    "side": str(getattr(position, "side", "")),
                    "quantity": str(getattr(position, "quantity", "")),
                    "status": getattr(result, "status", "unknown"),
                }
            )
            log.info(
                "close_all_positions_closed",
                symbol=symbol,
                status=getattr(result, "status", "unknown"),
            )

        # 6. Verify flat on the exchange: a "successful" submit is not proof
        #    the position is gone (e.g. -1102 queries left it unseen, or the
        #    close was accepted but a bracket re-opened it).  Only report
        #    success once no OPEN position remains.
        flat = False
        try:
            remaining = await self._exchange_adapter.get_positions()
            from quad.types.domain import PositionStatus as _PS

            still_open = [
                p for p in remaining if getattr(p, "status", None) == _PS.OPEN
            ]
            flat = len(still_open) == 0
            if still_open:
                log.warning(
                    "close_all_positions_remaining",
                    symbols=[
                        getattr(p, "symbol", "") or getattr(p, "contract_symbol", "")
                        for p in still_open
                    ],
                )
        except Exception as exc:
            log.warning("close_all_positions_verify_failed", error=str(exc))

        log.info(
            "close_all_positions_complete",
            total=len(open_positions),
            closed=success_count,
            failed=fail_count,
            flat_verified=flat,
        )

        return flat and fail_count == 0

    async def _execute_ai_action(
        self,
        decision: dict[str, Any],
        context: StrategyContext,
    ) -> bool:
        """Execute an AI-generated trading action through risk and execution.

        Parameters
        ----------
        decision:
            The parsed trading decision dict from the LLM.
        context:
            The current strategy context for risk evaluation.

        Returns
        -------
        bool
            ``True`` after the order is submitted successfully; ``False``
            on HOLD, incomplete decision, risk rejection/error, or
            execution exception.  Pair-rotation uses this to distinguish
            "ENTER opened a position" from "ENTER was rejected".
        """
        if self._risk_manager is None or self._execution_engine is None:
            self._log.warning("ai_execution_subsystems_missing")
            return False

        action_type = decision.get("action", "HOLD")
        if action_type == "HOLD":
            self._log.debug("ai_decision_hold", reason=decision.get("reasoning", ""))
            return False

        strategy_name = decision.get("strategy") or "ai_default"
        contract_symbol = decision.get("contract")
        quantity = decision.get("quantity")
        # All entries/exits are MARKET — never let the AI pick a limit order.
        order_type = "MARKET"
        limit_price = None  # market orders carry no limit price

        if not contract_symbol or not quantity:
            self._log.warning(
                "ai_decision_incomplete",
                contract=contract_symbol,
                quantity=quantity,
            )
            return False

        # ----------------------------------------------------------------
        # Phase 1 mandatory backstop: re-derive the order side deterministically
        # for ENTER/EXIT.  NEVER fall through to Action.__post_init__ defaults,
        # which would silently invert (ENTER->BUY, EXIT->SELL) and re-introduce
        # the exact long/short inversion bug this upgrade eliminates.
        # ----------------------------------------------------------------
        side = decision.get("side")
        if action_type in ("ENTER", "EXIT"):
            from quad.ai.validator import canonical_direction, derive_side

            direction = canonical_direction(decision.get("direction"))
            position_side = self._position_side_for_symbol(context, contract_symbol)
            derived_side = derive_side(action_type, direction, position_side)
            if not derived_side:
                self._log.warning(
                    "ai_side_un_derivable",
                    action=action_type,
                    direction=direction,
                    contract=contract_symbol,
                    position_side=getattr(position_side, "name", position_side),
                )
                return False
            if side not in (None, "") and str(side).strip().upper() != derived_side:
                self._log.warning(
                    "ai_side_derived_overrides",
                    action=action_type,
                    ai_side=side,
                    derived_side=derived_side,
                    contract=contract_symbol,
                )
            side = derived_side

        if not side:
            self._log.warning(
                "ai_decision_incomplete",
                contract=contract_symbol,
                side=side,
                quantity=quantity,
            )
            return False

        # One-trade-per-cycle: before ANY new ENTER, every existing position
        # must be closed and the account confirmed flat.  This is the hard
        # invariant for the user's "one trade in, one trade out" rule -- it
        # must not rely on the cycle-start force-close alone, because a close
        # can fail (or a stale local position list can hide a live position).
        if action_type == "ENTER":
            if self._config_dict.get("trading", {}).get("serial_trade_mode", True):
                closed = await self._close_all_positions()
                if not closed:
                    self._log.warning(
                        "ai_enter_blocked_positions_not_flat",
                        contract=contract_symbol,
                    )
                    return False
            else:
                # Even with serial mode disabled, never stack a second
                # position: if a position is open and we cannot confirm it is
                # closed, refuse the ENTER.
                try:
                    live = await self._exchange_adapter.get_positions()
                    from quad.types.domain import PositionStatus as _PS

                    open_now = [
                        p
                        for p in live
                        if getattr(p, "status", None) == _PS.OPEN
                    ]
                except Exception:
                    open_now = []
                if open_now:
                    self._log.warning(
                        "ai_enter_blocked_positions_open",
                        contract=contract_symbol,
                        open_symbols=[
                            getattr(p, "symbol", "")
                            or getattr(p, "contract_symbol", "")
                            for p in open_now
                        ],
                    )
                    return False

        self._log.debug(
            "ai_executing_action",
            action=action_type,
            contract=contract_symbol,
            side=side,
        )

        # Attach per-position TP/SL bracket prices on ENTER so the execution
        # engine places STOP_LOSS + TAKE_PROFIT orders alongside the market
        # entry.  The position is then closed ONLY by those brackets.
        stop_loss_price: Decimal | None = None
        take_profit_price: Decimal | None = None
        if action_type == "ENTER":
            stop_loss_price, take_profit_price = await self._compute_bracket_prices(
                contract_symbol, side
            )
            # A trade must never open without its SL/TP when the feature is
            # enabled.  Prices are (None, None) only when every bracket is
            # disabled or when the mark price could not be fetched -- in the
            # latter case refuse the ENTER instead of opening a bare position.
            risk_cfg = self._config_dict.get("risk", {})
            sl_enabled = bool(risk_cfg.get("per_position_sl", {}).get("enabled", True))
            tp_enabled = bool(risk_cfg.get("per_position_tp", {}).get("enabled", True))
            if sl_enabled or tp_enabled:
                if stop_loss_price is None or take_profit_price is None:
                    self._log.warning(
                        "ai_enter_blocked_missing_brackets",
                        contract=contract_symbol,
                        stop_loss=str(stop_loss_price),
                        take_profit=str(take_profit_price),
                    )
                    return False

        # Build Action dataclass
        from quad.types.risk import Action

        action = Action(
            type=action_type,
            strategy=strategy_name,
            symbol=contract_symbol,
            contract=contract_symbol,
            side=side,
            # Preserve the exact AI quantity (e.g. 0.005).  int() would
            # truncate fractional quantities to 0, silently zeroing the order.
            quantity=Decimal(str(quantity)),
            order_type=order_type,
            price=(Decimal(str(limit_price)) if limit_price is not None else None),
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            reason=decision.get("reasoning", "AI trading decision"),
            # fallback matches AiConfig.default_confidence schema default
            metadata={
                "ai_confidence": decision.get(
                    "confidence",
                    self._config_dict.get("ai", {}).get("default_confidence", 0.8),
                ),
            },
        )
        # Attach the position entry price on EXIT so the execution engine can
        # persist the realized PnL for this closing trade.
        if action_type == "EXIT":
            try:
                ctx_positions = getattr(context, "positions", None) or []
                held_ctx = next(
                    (
                        p
                        for p in ctx_positions
                        if (getattr(p, "symbol", "") or getattr(p, "contract_symbol", ""))
                        == contract_symbol
                    ),
                    None,
                )
                action.metadata["entry_price"] = str(
                    getattr(held_ctx, "entry_price", 0) or 0
                )
            except Exception:
                action.metadata["entry_price"] = "0"

        # Risk check
        try:
            result = await self._risk_manager.evaluate(action, context)
            if not result.passed:
                self._log.warning(
                    "ai_action_rejected_by_risk",
                    action=action_type,
                    contract=contract_symbol,
                    reason=result.reason,
                    gate=result.gate,
                )
                return False
            # Use the risk-sized action (Fix #4): RiskManager.evaluate returns a
            # possibly-reduced quantity in details["action"].  Record the
            # pre-sizing quantity so the execution engine can floor a
            # sub-minQty sized quantity up to the exchange minimum without
            # exceeding the AI's original request (the "pre-cap").
            sized_action = result.details.get("action", action)
            sized_action.risk_checked = True
            sized_action.metadata = {
                **(sized_action.metadata or {}),
                "pre_size_quantity": str(action.quantity),
            }
        except Exception as exc:
            self._log.exception("ai_risk_evaluation_error", error=str(exc))
            return False

        # Execute
        try:
            order_result = await self._execution_engine.execute(sized_action, context)
            self._log.info(
                "ai_order_executed",
                action=action_type,
                strategy=strategy_name,
                contract=contract_symbol,
                side=side,
                status=getattr(order_result, "status", "unknown"),
            )

            # Inspect the exchange-side status before treating the action as
            # successful.  The execution engine returns ``REJECTED`` (via
            # ``_rejected_result``) when the dry-run guard trips, the risk gate
            # rejects, quantity normalization fails, or submission raises.
            # Returning ``True`` for a rejected order would make the rotation
            # loop advance as if a position opened — producing phantom trades,
            # skipped rotation cycles, and stale ``outcome='open'`` rows.
            # Mirror the confirmation logic in ``_close_all_positions``.
            order_status = getattr(order_result, "status", "unknown")
            if order_status not in ("FILLED", "NEW", "PARTIALLY_FILLED"):
                self._log.warning(
                    "ai_action_not_confirmed",
                    action=action_type,
                    contract=contract_symbol,
                    status=order_status,
                )
                return False

            # Mark the logged decision row as executed so the Phase-3 metrics
            # / prompt context (prompt.py counts ``executed``) reflects reality.
            # ``decision["db_id"]`` is stashed by _log_ai_decision; when the
            # DB path was skipped it is absent and we simply no-op.
            decision_id = decision.get("db_id")
            if decision_id is not None and self._db_manager is not None:
                try:
                    from quad.persistence.repositories import DecisionRepository

                    DecisionRepository(self._db_manager).update(
                        decision_id, executed=1
                    )
                except Exception:
                    self._log.warning(
                        "ai_decision_executed_flag_update_failed",
                        decision_id=decision_id,
                    )

            # Notify on successful execution (ENTER, EXIT, ADJUST, ROLL).
            # ENTER alerts always include the computed SL/TP brackets.
            if action_type in ("ENTER", "EXIT"):
                exit_pnl: str | None = None
                if action_type == "EXIT":
                    # Pass the entry price stashed on the EXIT action's
                    # metadata (captured from the live position at decision
                    # time) as a fallback for stale position books.
                    entry_hint = Decimal(
                        str(sized_action.metadata.get("entry_price", "0") or "0")
                    ) or None
                    exit_pnl = await self._build_exit_pnl_text(
                        contract_symbol,
                        side,
                        Decimal(str(sized_action.quantity or 0)),
                        order_result,
                        entry_price_hint=entry_hint,
                    )
                await self._notify_trade(
                    action_type=action_type,
                    strategy=strategy_name,
                    contract=contract_symbol,
                    side=side,
                    # Use the sized/final quantity.  int() would truncate
                    # fractional quantities (e.g. 0.005) to 0 in the
                    # notification.
                    quantity=str(sized_action.quantity),
                    price=str(action.price) if action.price else None,
                    reason=action.reason,
                    stop_loss=stop_loss_price,
                    take_profit=take_profit_price,
                    pnl=exit_pnl,
                )
            return True
        except Exception as exc:
            self._log.exception(
                "ai_order_execution_error",
                action=action_type,
                contract=contract_symbol,
                error=str(exc),
            )
            return False

    async def _build_exit_pnl_text(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        order_result: Any,
        entry_price_hint: Decimal | None = None,
    ) -> str | None:
        """Build the ``$x.xx (y%)`` PnL line for a closed position.

        Uses the exchange fill price when available, otherwise the live mark
        price, against the position's stored entry price.  Returns ``None``
        (no PnL line) when no entry price or exit price can be derived.

        ``entry_price_hint`` is the entry price captured on the closing
        action's metadata at decision time (see ``_execute_ai_action`` EXIT
        branch).  It is used as a fallback when the local position book is
        stale / the position has already been removed from the exchange's
        open-positions list at the moment of the EXIT — the common case where
        the bot closes a position and the exchange drops it before the PnL
        notification fires.
        """
        try:
            positions = await self._exchange_adapter.get_positions()
            held = next(
                (
                    p
                    for p in positions
                    if (getattr(p, "symbol", "") or getattr(p, "contract_symbol", ""))
                    == symbol
                ),
                None,
            )
            # Primary source: the live position's entry price.  Fallback:
            # the entry price stashed on the EXIT action's metadata (captured
            # before submission), so a stale/no position still yields PnL.
            raw_entry = getattr(held, "entry_price", 0) if held is not None else None
            if not raw_entry and entry_price_hint is not None:
                raw_entry = entry_price_hint
            entry_price = Decimal(str(raw_entry or 0))
            # Prefer the exchange fill price; fall back to the mark price.
            exit_price = Decimal(0)
            fills = getattr(order_result, "fills", None) or []
            if fills:
                try:
                    exit_price = Decimal(str(fills[-1].get("price", "0")))
                except (TypeError, ValueError):
                    exit_price = Decimal(0)
            if not exit_price:
                mark = await self._exchange_adapter.get_mark_price(symbol)
                exit_price = Decimal(str(mark))
            if not entry_price or not exit_price:
                return None
            pnl = self._compute_position_pnl(
                entry_price=entry_price,
                exit_price=exit_price,
                quantity=quantity,
                side=(str(getattr(held, "side", "")) or side),
            )
            return self._format_pnl(pnl, entry_price)
        except Exception as exc:
            self._log.warning("ai_exit_pnl_compute_failed", symbol=symbol, error=str(exc))
            return None

    async def _compute_bracket_prices(
        self,
        symbol: str,
        side: str,
    ) -> tuple[Decimal | None, Decimal | None]:
        """Compute per-position stop-loss / take-profit prices for an ENTER.

        Uses the same formula as ``StrategyBase._build_tp_sl_actions``: for a
        fixed SL/TP the price offset is ``capital_pct / 100 / leverage``
        applied to the current mark price (the market entry price).  Prices
        are ``None`` when the feature is disabled or the mark price is
        unavailable, in which case no bracket orders are placed.

        Parameters
        ----------
        symbol:
            Contract symbol being entered, e.g. ``"BTCUSDT"``.
        side:
            Entry side, ``"buy"``/``"sell"`` or ``"BUY"``/``"SELL"``.

        Returns
        -------
        tuple[Decimal | None, Decimal | None]
            ``(stop_loss_price, take_profit_price)``.
        """
        risk_cfg = self._config_dict.get("risk", {})
        sl_cfg = risk_cfg.get("per_position_sl", {})
        tp_cfg = risk_cfg.get("per_position_tp", {})
        if not sl_cfg.get("enabled", True) and not tp_cfg.get("enabled", True):
            return None, None

        try:
            mark = await self._exchange_adapter.get_mark_price(symbol)
            entry = Decimal(str(mark))
        except Exception as exc:
            self._log.warning(
                "ai_bracket_price_unavailable",
                symbol=symbol,
                error=str(exc),
            )
            return None, None
        if entry <= 0:
            self._log.warning(
                "ai_bracket_price_invalid",
                symbol=symbol,
                mark=str(entry),
            )
            return None, None

        leverage = Decimal(
            str(self._config_dict.get("trading", {}).get("leverage", 50))
        )
        sl_pct = Decimal(str(sl_cfg.get("capital_pct", 30.0)))
        tp_pct = Decimal(str(tp_cfg.get("capital_pct", 50.0)))
        is_long = side.strip().upper() in ("BUY", "LONG")

        sl_price: Decimal | None = None
        tp_price: Decimal | None = None
        if sl_cfg.get("enabled", True):
            offset = sl_pct / Decimal(100) / leverage
            sl_price = (
                entry * (Decimal(1) - offset)
                if is_long
                else entry * (Decimal(1) + offset)
            )
        if tp_cfg.get("enabled", True):
            offset = tp_pct / Decimal(100) / leverage
            tp_price = (
                entry * (Decimal(1) + offset)
                if is_long
                else entry * (Decimal(1) - offset)
            )
        return sl_price, tp_price

    async def _log_ai_decision(
        self,
        decision: dict[str, Any],
        context: Any,
    ) -> int | None:
        """Log an AI decision to the database DecisionModel table.

        Returns the generated ``decision_id`` (or ``None`` when the DB is
        unavailable / the INSERT failed) so callers can later mark the row
        as ``executed=1`` after a confirmed fill.
        """
        if self._db_manager is None:
            return None

        from quad.persistence.models import DecisionModel
        from quad.persistence.repositories import DecisionRepository

        repo = DecisionRepository(self._db_manager)
        try:
            # Confidence arrives as a float after normalize_decision, but the
            # legacy path may carry a raw string; coerce defensively.
            try:
                confidence = float(decision.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            action = decision.get("action") or "HOLD"
            symbol = decision.get("contract") or ""
            # Phase 3: capture the mark price at decision time for ENTER
            # decisions so realized PnL / exit can be computed later once a
            # close source exists.  ``context.mark_prices`` is keyed by pair
            # symbol and is already available without an extra API call.
            entry_price = ""
            if action == "ENTER":
                try:
                    mark_prices = getattr(context, "mark_prices", None) or {}
                    entry_price = str(mark_prices.get(symbol) or "")
                except Exception:
                    entry_price = ""
            decision_id = await repo.create(
                DecisionModel(
                    id=0,  # auto-generated by AUTOINCREMENT
                    timestamp=int(time.time()),
                    # The AI JSON may carry an EXPLICIT null for these fields
                    # (e.g. `"strategy": "trend_following" | null`). `dict.get()`
                    # returns that null instead of the fallback, which would
                    # violate the NOT NULL columns in `decisions` DDL.  Coerce
                    # null-ish values (None / "") back to the fallback here so
                    # the repository INSERT never hits the constraint.
                    strategy=decision.get("strategy") or "ai_default",
                    action=action,
                    symbol=symbol,
                    reason=(decision.get("reasoning") or "")[:500],
                    risk_passed=1 if action in ("ENTER", "EXIT") else 0,
                    executed=0,
                    cycle_time_ms=int(self._last_ai_cycle_time_ms),
                    # Phase 1 fields: predicted direction + plausibility gate.
                    predicted_direction=decision.get("direction") or "NEUTRAL",
                    confidence=confidence,
                    gate_result=decision.get("gate_result") or "not_checked",
                    # Phase 3: mark price at decision time (empty when the
                    # context had no mark price for the symbol).
                    entry_price=entry_price,
                    # Only an ENTER decision opens a position worth tracking to
                    # resolution; every other action is inherently "no open
                    # position from this decision".
                    outcome="open" if action == "ENTER" else "flat",
                )
            )
            # Stash the DB id back on the decision dict so the execution
            # path (_execute_ai_action) can flip `executed=1` after a
            # confirmed fill, instead of leaving every row at 0.
            decision["db_id"] = decision_id
            return decision_id
        except Exception as exc:
            self._log.warning("ai_decision_db_log_error", error=str(exc))
            return None

    async def _reconcile_decision_outcomes(self, positions: Any) -> None:
        """Resolve open ENTER decision outcomes against the live position set.

        There is no repository-level close path: positions close on the
        exchange via the STOP_LOSS / TAKE_PROFIT bracket orders attached at
        entry (``execution.engine``), and the fill reconciler only detects
        discrepancies.  So each cycle we compare unresolved ENTER decisions
        (``outcome='open'``) against the live open positions and mark any
        whose symbol no longer has an open position as resolved.

        The close happened off-process, so realized PnL / exit price are not
        available locally; the outcome is marked ``"flat"`` so the row stops
        being treated as open.  The Phase-2 metrics module can backfill actual
        win/loss from trade history.

        Parameters
        ----------
        positions:
            The live open positions fetched this cycle.
        """
        if self._db_manager is None:
            return

        from quad.persistence.repositories import DecisionRepository
        from quad.types.domain import PositionStatus

        open_symbols = {
            (getattr(p, "symbol", "") or getattr(p, "contract_symbol", ""))
            for p in positions
            if getattr(p, "status", None) == PositionStatus.OPEN
        }

        repo = DecisionRepository(self._db_manager)
        unresolved = await repo.get_unresolved()
        now = int(time.time())
        for dec in unresolved:
            if dec.symbol in open_symbols:
                continue  # position still open; keep unresolved
            self._log.info(
                "decision_outcome_resolved",
                decision_id=dec.id,
                symbol=dec.symbol,
                action=dec.action,
            )
            await repo.mark_outcome(
                decision_id=dec.id,
                outcome="flat",
                resolved_at=now,
            )

    async def _compute_ai_metrics(self) -> None:
        """Compute and log prediction-quality metrics over resolved decisions.

        Phase 3: pulls resolved (non-``'open'``) decisions via
        ``DecisionRepository.get_resolved``, feeds them to
        ``quad.ai.metrics.compute_metrics`` (hit rate, Expected Calibration
        Error, Brier score), and logs the result on a config-gated interval.
        See ``quad.ai.metrics`` for the win/loss/flat semantics.

        Cost control (kept deliberately lightweight):
        - Runs every ``metrics.interval_cycles`` main cycles (default 1).
        - Skips logging when fewer than ``metrics.min_resolved`` directional
          rows exist (default 5).
        - The work is one indexed SELECT plus in-memory arithmetic — no
          network, no exchange calls.

        Known limitation (flagged intentionally): ``_reconcile_decision_outcomes``
        marks every disappeared symbol ``'flat'``, conflating "closed at
        breakeven" with "symbol rotation moved on while the decision was still
        open".  Because realized PnL is not backfilled (no income-history API,
        no local close path), win/loss labels are not yet assigned, so the
        directional count is typically 0 and the ratio metrics log as ``None``
        until a real close source lands.
        """
        if self._db_manager is None:
            return

        metrics_cfg = self._config_dict.get("ai", {}).get("metrics", {})
        if not metrics_cfg.get("enabled", True):
            return

        interval = int(metrics_cfg.get("interval_cycles", 1) or 1)
        interval = max(interval, 1)
        if self._metrics_cycle_count % interval != 0:
            return

        min_resolved = int(metrics_cfg.get("min_resolved", 5) or 0)
        only_directional = bool(metrics_cfg.get("only_directional", True))

        try:
            from quad.ai.metrics import compute_metrics
            from quad.persistence.repositories import DecisionRepository

            repo = DecisionRepository(self._db_manager)
            resolved = await repo.get_resolved(
                limit=2000,
                only_directional=only_directional,
            )
            m = compute_metrics(resolved)
            if m.directional_count < min_resolved:
                self._log.debug(
                    "ai_metrics_skipped",
                    directional_count=m.directional_count,
                    min_resolved=min_resolved,
                    resolved_count=m.resolved_count,
                    flat_count=m.flat_count,
                    open_count=m.open_count,
                )
                return

            self._log.info(
                "ai_decision_metrics",
                sample_count=m.sample_count,
                resolved_count=m.resolved_count,
                directional_count=m.directional_count,
                wins=m.wins,
                losses=m.losses,
                flat_count=m.flat_count,
                open_count=m.open_count,
                hit_rate=round(m.hit_rate, 4) if m.hit_rate is not None else None,
                ece=round(m.ece, 4) if m.ece is not None else None,
                brier=round(m.brier, 4) if m.brier is not None else None,
                mean_confidence=(
                    round(m.mean_confidence, 4)
                    if m.mean_confidence is not None
                    else None
                ),
                n_bins=len(m.ece_bins),
            )

            if self._metrics is not None:
                self._metrics.set_gauge(
                    "ai_hit_rate",
                    m.hit_rate if m.hit_rate is not None else float("nan"),
                )
                self._metrics.set_gauge(
                    "ai_ece",
                    m.ece if m.ece is not None else float("nan"),
                )
                self._metrics.set_gauge(
                    "ai_brier",
                    m.brier if m.brier is not None else float("nan"),
                )
                self._metrics.set_gauge(
                    "ai_decisions_resolved", float(m.resolved_count)
                )
                self._metrics.set_gauge(
                    "ai_decisions_directional", float(m.directional_count)
                )
        except Exception as exc:
            self._log.warning("ai_metrics_compute_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Telegram trade notifications
    # ------------------------------------------------------------------

    @staticmethod
    def _side_label(side: Any) -> str:
        """Normalize a position/order side to ``LONG``/``SHORT``/``BUY``/``SELL``.

        Binance positions carry ``PositionSide.LONG`` enums while order sides
        are ``BUY``/``SELL`` strings; the Telegram alert should never show the
        raw ``PositionSide.LONG`` repr.
        """
        if side is None:
            return ""
        from quad.types.domain import PositionSide as PS

        if isinstance(side, PS):
            return side.value
        text = str(side).strip()
        if text.startswith("PositionSide."):
            text = text.split(".", 1)[1]
        return text.upper()

    @staticmethod
    def _compute_position_pnl(
        entry_price: Decimal | None,
        exit_price: Decimal | None,
        quantity: Decimal,
        side: str,
    ) -> Decimal:
        """Realized PnL for a closed position in quote (USDT) terms.

        ``(exit - entry) * qty`` for LONG, ``(entry - exit) * qty`` for
        SHORT.  Returns ``Decimal(0)`` when the prices are unavailable so a
        close never fails on missing data.
        """
        try:
            if entry_price is None or exit_price is None or not quantity:
                return Decimal(0)
            is_long = str(side or "").strip().upper() in ("BUY", "LONG")
            diff = exit_price - entry_price
            if not is_long:
                diff = -diff
            return diff * quantity
        except Exception:
            return Decimal(0)

    @staticmethod
    def _format_pnl(pnl: Decimal, entry_price: Decimal | None) -> str:
        """Format realized PnL as ``$x.xx (y.y%)``.

        The percentage is relative to the entry notional (``entry * qty`` is
        unavailable here, so it is relative to the entry price instead);
        pass ``None`` to omit the percentage.
        """
        try:
            amount = f"${float(pnl):,.2f}"
            if entry_price and entry_price > 0:
                pct = float(pnl) / float(entry_price) * 100.0
                return f"{amount} ({pct:+.2f}%)"
            return amount
        except Exception:
            return f"${float(pnl):,.2f}"

    async def _notify_trade(
        self,
        action_type: str,
        strategy: str,
        contract: str,
        side: str,
        quantity: str,
        price: str | None,
        reason: str,
        pnl: str | None = None,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
    ) -> None:
        """Send trade notification via Telegram."""
        if not getattr(self, "_telegram_bot", None) or not getattr(
            self, "_telegram_chat_id", 0
        ):
            return
        try:
            emoji = {
                "ENTER": "\U0001f7e2",
                "EXIT": "\U0001f534",
                "ADJUST": "\U0001f7e1",
                "ROLL": "\U0001f504",
            }.get(action_type, "⚪")
            esc = _html.escape
            msg = (
                f"{emoji} <b>{esc(action_type)}</b> | {esc(strategy)}\n"
                f"Contract: <code>{esc(contract)}</code>\n"
                f"Side: {esc(side)} | Qty: {esc(quantity)}\n"
                f"Price: {esc(price or 'MARKET')}\n"
            )
            if stop_loss is not None and take_profit is not None:
                msg += (
                    f"SL: <code>{esc(str(stop_loss))}</code> | "
                    f"TP: <code>{esc(str(take_profit))}</code>\n"
                )
            if pnl:
                msg += f"PnL: {esc(pnl)}\n"
            msg += f"Reason: {esc(reason)}"
            await self._telegram_bot.send_message(
                chat_id=self._telegram_chat_id,
                text=msg,
                parse_mode="HTML",
            )
        except Exception as exc:
            self._log.warning("telegram_notify_failed", error=str(exc))

    async def _notify_circuit_breaker(self, name: str, reason: str, tier: int) -> None:
        """Send circuit breaker alert via Telegram."""
        if not getattr(self, "_telegram_bot", None) or not getattr(
            self, "_telegram_chat_id", 0
        ):
            return
        try:
            esc = _html.escape
            msg = (
                f"\U0001f6a8 <b>Circuit Breaker Triggered</b>\n"
                f"Name: <code>{esc(name)}</code>\n"
                f"Tier: {esc(str(tier))}\n"
                f"Reason: {esc(reason)}"
            )
            await self._telegram_bot.send_message(
                chat_id=self._telegram_chat_id,
                text=msg,
                parse_mode="HTML",
            )
        except Exception as exc:
            self._log.warning("telegram_cb_notify_failed", error=str(exc))

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


def _dot_get(d: dict[str, Any], key: str, default: Any = None) -> Any:
    """Simple dot-notation lookup (copied from config.manager for isolation)."""
    if not key:
        return default
    parts = key.split(".")
    current: Any = d
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return default
    return current


