"""QuadOrchestrator — top-level application coordinator.

Wires all subsystems together and manages the full trading lifecycle:

    - Configuration loading (4-layer merge)
    - Database initialization and migrations
    - Exchange adapter creation (binance / paper / mock)
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
import os
import signal
import sys
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

from quad.config.manager import ConfigManager
from quad.config.schema import AiConfig, TradingViewWebhookConfig
from quad.exchange.factory import create_exchange
from quad.market_data.engine import MarketDataEngine
from quad.persistence.database import DatabaseManager
from quad.risk.manager import RiskManager
from quad.execution.engine import ExecutionEngine
from quad.strategy.factory import create_default_strategies
from quad.strategy.base import StrategyBase
from quad.types.strategy import StrategyContext

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = "config/config.local.yaml"
_DEFAULT_CYCLE_INTERVAL = 60  # seconds (deterministic fallback)
_DEFAULT_AI_CYCLE_INTERVAL = 3600  # seconds (1 hour for AI-first mode)

# Default underlyings to fetch option chains for
_DEFAULT_UNDERLYINGS = ["BTCUSDT", "ETHUSDT"]


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
        should also contain ``config.default.yaml``.

    Attributes
    ----------
    _mode : str
        Resolved trading mode: ``"paper"``, ``"live"``, or ``"dry_run"``.
    _stop_event : asyncio.Event
        Set when a shutdown signal is received.
    """

    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH) -> None:
        self._log = logger.bind()

        # Config path
        config_dir = str(Path(config_path).parent.resolve())
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
        self._tv_webhook: Any = None

        # Cached config dict (used by multiple subsystems)
        self._config_dict: dict[str, Any] = {}
        self._mode: str = "paper"
        self._cycle_interval: int = _DEFAULT_CYCLE_INTERVAL

        # AI-first mode tracking
        self._ai_cycle_interval: int = _DEFAULT_AI_CYCLE_INTERVAL
        self._ai_enabled: bool = False
        self._ai_cycle_count: int = 0
        self._last_ai_decision: dict[str, Any] = {}
        self._last_ai_error: str | None = None
        self._last_ai_cycle_time_ms: float = 0.0
        self._consecutive_ai_failures: int = 0

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
        2. DatabaseManager (connect + initialize + migrate)
        3. ExchangeAdapter (via factory)
        4. MarketDataEngine
        5. RiskManager
        6. ExecutionEngine
        7. Strategy system
        8. QuadBot (Telegram)
        9. HealthServer
        10. MetricsCollector

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
            await self._init_database()
            await self._init_exchange_adapter()
            await self._init_market_data()
            await self._init_risk_manager()
            await self._init_execution_engine()
            await self._init_strategies()
            await self._init_telegram_bot()
            await self._init_health_server()
            await self._init_metrics()
            await self._init_groq_ai()
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
            self._config_manager.get("trading.max_cycle_interval", _DEFAULT_CYCLE_INTERVAL)
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

        # Telegram admin IDs
        admin_ids_str = os.environ.get("TELEGRAM_ADMIN_IDS")
        if admin_ids_str:
            try:
                ids = [
                    int(x.strip())
                    for x in admin_ids_str.split(",")
                    if x.strip()
                ]
                self._set_telegram_config("admin_ids", ids)
            except (ValueError, TypeError):
                self._log.warning(
                    "invalid_telegram_admin_ids",
                    value=admin_ids_str,
                )

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
            The config key to set (e.g. ``"bot_token"``, ``"admin_ids"``).
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
        dsn = self._config_manager.get(
            "persistence.dsn",
            os.environ.get("DATABASE_URL", "postgresql://quad:quad@localhost:5432/quad"),
        )
        busy_timeout = self._config_manager.get("persistence.busy_timeout", 5000)

        self._db_manager = DatabaseManager(
            dsn=str(dsn),
            busy_timeout=int(busy_timeout),
        )
        await self._db_manager.connect()
        await self._db_manager.initialize()
        await self._db_manager.migrate()
        self._log.info("database_initialized", dsn=self._db_manager.dsn)

    async def _init_exchange_adapter(self) -> None:
        """Create and connect the exchange adapter.

        Maps ``QUAD_MODE`` to the exchange implementation:
            - ``"paper"`` / ``"dry_run"`` -> ``PaperTradingAdapter``
            - ``"live"`` -> configured exchange (default ``BinanceOptionsAdapter``)
        """
        # Override exchange name based on mode
        mode = self._mode
        exchange_cfg: dict[str, Any] = dict(
            self._config_dict.get("exchange", {})
        )

        if mode in ("paper", "dry_run"):
            exchange_cfg["name"] = "paper"
        elif mode == "live":
            exchange_cfg["name"] = exchange_cfg.get("name", "binance")

        # Ensure rate_limit is a dict (for Binance adapter)
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
            exchange_name=exchange_cfg.get("name", "unknown"),
        )

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

    async def _init_telegram_bot(self) -> None:
        """Initialise the Telegram bot (if enabled and configured)."""
        telegram_cfg = self._config_dict.get("telegram", {})
        if not telegram_cfg.get("bot_token"):
            self._log.info("telegram_bot_disabled_no_token")
            self._bot = None
            return

        if not telegram_cfg.get("enabled", True):
            self._log.info("telegram_bot_disabled_config")
            self._bot = None
            return

        # Lazy import to avoid PTB import errors when token is missing
        from quad.bot.bot import QuadBot

        self._bot = QuadBot(
            config=self._config_dict,
            orchestrator=self,
            risk_manager=self._risk_manager,
            execution_engine=self._execution_engine,
            market_data_engine=self._market_data,
            db_manager=self._db_manager,
            groq_client=self._groq_client,
        )
        await self._bot.start()
        self._log.info("telegram_bot_initialized")

    async def _init_health_server(self) -> None:
        """Initialise the health check HTTP server."""
        monitoring_cfg = self._config_dict.get("monitoring", {})
        health_cfg = monitoring_cfg.get("health_server", {})
        port = int(
            health_cfg.get("port")
            or os.environ.get("QUAD_HEALTH_PORT", "9090")
        )
        enabled = health_cfg.get("enabled", True)

        if not enabled:
            self._log.info("health_server_disabled_config")
            self._health_server = None
            return

        from quad.monitoring.health import HealthServer

        self._health_server = HealthServer(
            port=port,
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
        self._log.info("metrics_collector_initialized")

    async def _init_groq_ai(self) -> None:
        """Initialise the Groq AI client (if API key is available)."""
        ai_cfg = AiConfig.model_validate(self._config_dict.get("ai", {}))
        api_key = os.environ.get("GROQ_API_KEY") or self._config_dict.get("ai", {}).get("api_key", "")

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
        )

        # Set AI cycle interval from config, defaulting to 1 hour
        self._ai_cycle_interval = int(
            self._config_manager.get(
                "trading.ai_cycle_interval", _DEFAULT_AI_CYCLE_INTERVAL
            )
            if self._config_manager
            else _DEFAULT_AI_CYCLE_INTERVAL
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
                    "or the QUAD_TRADINGVIEW_WEBHOOK_SECRET env var."
                ),
            )

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
                    payload = _json.loads(raw_text) if raw_text.strip().startswith("{") else {}
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
                    from dataclasses import dataclass

                    @dataclass
                    class _Action:
                        type: str
                        strategy: str
                        contract: str
                        side: str
                        quantity: Any
                        price: Any
                        reason: str
                        metadata: Any

                    action = _Action(
                        type="ENTER",
                        strategy=f"tradingview_{signal.strategy_name}",
                        contract=signal.symbol,
                        side=signal.side,
                        quantity=signal.quantity,
                        price=signal.price,
                        reason=f"TradingView alert: {signal.strategy_name}",
                        metadata=signal.metadata,
                    )
                    await self._execution_engine.execute(action, {})
                except Exception as exc:
                    log.exception("tv_webhook_execution_error", error=str(exc))

            return web.json_response({"status": "ok"})

        # Register the route on the health server
        self._health_server.add_route("POST", "/webhook/tradingview", _tv_webhook_handler)

        self._tv_webhook = {"enabled": True, "secret_configured": bool(secret), "port": port}
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

        AI-First Flow (when AI is enabled and client is available):
        1. Collect full market context (candles, positions, account, chains)
        2. Compute technical indicators from candle data
        3. Build structured prompts for Groq LLM
        4. Call ``decide_trades()`` on the Groq client
        5. Parse the AI decision into an ``Action``
        6. Pass through risk manager
        7. Execute if risk checks pass
        8. Log decision to database
        9. Fallback to deterministic strategies on AI failure

        Deterministic Fallback (when AI is disabled or unavailable):
        Original flow: evaluate registered strategies directly.
        """
        underlyings = list(
            self._config_manager.get("trading.underlyings", _DEFAULT_UNDERLYINGS)
            if self._config_manager
            else _DEFAULT_UNDERLYINGS
        )

        while not self._stop_event.is_set():
            cycle_start = time.monotonic()

            try:
                # ----------------------------------------------------------
                # 1. Account state
                # ----------------------------------------------------------
                account = await self._exchange_adapter.get_account()
                positions = await self._exchange_adapter.get_positions()
                open_orders = []
                try:
                    open_orders = await self._exchange_adapter.get_open_orders()
                except Exception:
                    pass  # Non-critical; continue with empty orders

                # ----------------------------------------------------------
                # 2. AI-First Decision (if enabled and available)
                # ----------------------------------------------------------
                ai_decision: dict[str, Any] = {}
                ai_used = False

                if self._ai_enabled and self._groq_client is not None:
                    try:
                        ai_available = self._groq_client.is_available()
                        if ai_available:
                            ai_decision = await self._run_ai_trading_cycle(
                                underlyings, account, positions
                            )
                            ai_used = True
                            self._consecutive_ai_failures = 0
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
                # 3. Option chains (for deterministic fallback)
                # ----------------------------------------------------------
                all_chains: list[Any] = []
                for underlying in underlyings:
                    try:
                        chain = await self._market_data.get_option_chain(underlying)
                        all_chains.extend(chain)
                    except Exception as exc:
                        self._log.warning(
                            "option_chain_fetch_failed",
                            underlying=underlying,
                            error=str(exc),
                        )

                context = StrategyContext(
                    account=account,
                    positions=positions,
                    orders=open_orders,
                    option_chain=all_chains,
                    config=self._config_dict,
                )

                # ----------------------------------------------------------
                # 4. Execute AI action if valid, else fallback to strategies
                # ----------------------------------------------------------
                if ai_used and ai_decision.get("action") in ("ENTER", "EXIT"):
                    await self._execute_ai_action(ai_decision, context)
                else:
                    # Deterministic strategy fallback
                    all_actions: list[Any] = []
                    for name, strategy in self._active_strategies.items():
                        try:
                            strategy_params = self._config_dict.get(
                                "strategy", {}
                            ).get(name, {})
                            ctx = replace(context, strategy_params=strategy_params)
                            actions = await strategy.evaluate(ctx)
                            all_actions.extend(actions)
                        except Exception as exc:
                            self._log.error(
                                "strategy_evaluation_error",
                                strategy=name,
                                error=str(exc),
                            )

                    for action in all_actions:
                        try:
                            ctx_copy = replace(context)
                            result = await self._risk_manager.evaluate(
                                action, ctx_copy
                            )
                            if result.passed:
                                order_result = await self._execution_engine.execute(
                                    action, ctx_copy
                                )
                                self._log.info(
                                    "order_executed",
                                    strategy=action.strategy,
                                    contract=action.contract,
                                    side=action.side,
                                    status=order_result.status,
                                )
                            else:
                                self._log.warning(
                                    "action_rejected_by_risk",
                                    strategy=action.strategy,
                                    contract=action.contract,
                                    reason=result.reason,
                                    gate=result.gate,
                                )
                        except Exception as exc:
                            self._log.exception(
                                "action_execution_error",
                                strategy=action.strategy,
                                contract=action.contract,
                                error=str(exc),
                            )

                # ----------------------------------------------------------
                # 5. Update monitoring / metrics
                # ----------------------------------------------------------
                try:
                    await self._risk_manager.update_monitoring(context)
                except Exception as exc:
                    self._log.warning(
                        "risk_monitoring_update_error",
                        error=str(exc),
                    )

                if self._metrics is not None:
                    self._metrics.set_gauge(
                        "active_positions", float(len(positions))
                    )
                    self._metrics.set_gauge(
                        "active_strategies", float(len(self._active_strategies))
                    )
                    self._metrics.set_gauge(
                        "option_contracts_seen", float(len(all_chains))
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
                            float(getattr(account, "total_usdt", Decimal("0"))),
                        )

                # ----------------------------------------------------------
                # 6. Sleep for remaining interval
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

        # 1. Collect market context
        from quad.ai.context import collect_market_context

        context = await collect_market_context(
            exchange_adapter=self._exchange_adapter,
            market_data_engine=self._market_data,
            db_manager=self._db_manager,
            config=self._config_dict,
        )
        self._log.info(
            "market_context_collected",
            pairs=len(context.candles),
            positions=len(context.positions),
            chains=len(context.option_chains),
            errors=len(context.errors),
        )

        # 2. Compute technical indicators per pair/timeframe
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
        self._log.info(
            "ai_decision_request",
            cycle=self._ai_cycle_count,
            system_prompt_len=len(prompts["system"]),
            user_prompt_len=len(prompts["user"]),
        )

        decision = await self._groq_client.decide_trades(
            system_prompt=prompts["system"],
            user_prompt=prompts["user"],
            temperature=0.0,
            max_tokens=2048,
        )

        # Track timing
        self._last_ai_cycle_time_ms = round(
            (time.monotonic() - ai_start) * 1000, 2
        )
        self._last_ai_decision = decision

        # 5. Log decision to database
        try:
            await self._log_ai_decision(decision, context)
        except Exception as exc:
            self._log.warning("ai_decision_log_failed", error=str(exc))

        self._log.info(
            "ai_decision_received",
            action=decision.get("action", "unknown"),
            strategy=decision.get("strategy"),
            confidence=decision.get("confidence"),
            cycle_time_ms=self._last_ai_cycle_time_ms,
        )

        return decision

    async def _execute_ai_action(
        self,
        decision: dict[str, Any],
        context: StrategyContext,
    ) -> None:
        """Execute an AI-generated trading action through risk and execution.

        Parameters
        ----------
        decision:
            The parsed trading decision dict from the LLM.
        context:
            The current strategy context for risk evaluation.
        """
        action_type = decision.get("action", "HOLD")
        if action_type == "HOLD":
            self._log.info("ai_decision_hold", reason=decision.get("reasoning", ""))
            return

        strategy_name = decision.get("strategy") or "ai_default"
        contract_symbol = decision.get("contract")
        side = decision.get("side")
        quantity = decision.get("quantity")
        order_type = decision.get("order_type", "LIMIT")
        limit_price = decision.get("limit_price")

        if not contract_symbol or not side or not quantity:
            self._log.warning(
                "ai_decision_incomplete",
                contract=contract_symbol,
                side=side,
                quantity=quantity,
            )
            return

        # Build Action dataclass
        from quad.types.risk import Action

        action = Action(
            type=action_type,  # type: ignore[arg-type]
            strategy=strategy_name,
            contract=contract_symbol,
            side=side,
            quantity=int(quantity),
            order_type=order_type,
            price=(
                Decimal(str(limit_price)) if limit_price is not None else None
            ),
            reason=decision.get("reasoning", "AI trading decision"),
            metadata={"ai_confidence": decision.get("confidence", 0.0)},
        )

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
                return
        except Exception as exc:
            self._log.exception("ai_risk_evaluation_error", error=str(exc))
            return

        # Execute
        try:
            order_result = await self._execution_engine.execute(action, context)
            self._log.info(
                "ai_order_executed",
                action=action_type,
                strategy=strategy_name,
                contract=contract_symbol,
                side=side,
                status=getattr(order_result, "status", "unknown"),
            )
        except Exception as exc:
            self._log.exception(
                "ai_order_execution_error",
                action=action_type,
                contract=contract_symbol,
                error=str(exc),
            )

    async def _log_ai_decision(
        self,
        decision: dict[str, Any],
        context: Any,
    ) -> None:
        """Log an AI decision to the database DecisionModel table."""
        if self._db_manager is None:
            return

        from quad.persistence.models import DecisionModel
        from quad.persistence.repositories import DecisionRepository

        repo = DecisionRepository(self._db_manager)
        try:
            await repo.create(
                DecisionModel(
                    id=0,  # auto-generated by SERIAL
                    timestamp=int(time.time()),
                    strategy=decision.get("strategy", "ai_default"),
                    action=decision.get("action", "HOLD"),
                    contract_symbol=decision.get("contract", ""),
                    reason=decision.get("reasoning", "")[:500],
                    risk_passed=1 if decision.get("action") in ("ENTER", "EXIT") else 0,
                    executed=0,
                    cycle_time_ms=int(self._last_ai_cycle_time_ms),
                )
            )
        except Exception as exc:
            self._log.warning("ai_decision_db_log_error", error=str(exc))

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
        result: dict[str, Any] = {
            "orchestrator": {
                "started": self._started,
                "mode": self._mode,
                "cycle_interval_s": self._cycle_interval,
                "stop_event_set": self._stop_event.is_set(),
            },
            "config": {
                "loaded": self._config_manager is not None,
                "mode": self._mode,
            },
            "exchange": {
                "connected": (
                    getattr(self._exchange_adapter, "is_connected", False)
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
                    self._groq_client is not None
                    and self._groq_client.is_available()
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
                ),  # noqa: E501
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
