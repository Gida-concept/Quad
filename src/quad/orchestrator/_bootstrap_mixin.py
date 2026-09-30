"""Subsystem bootstrap for the orchestrator.

Every ``_init_*`` / ``_setup_*`` step that wires one component of the trading
stack, extracted verbatim from ``orchestrator.py`` -- all 18 methods' ASTs are
unchanged; only the enclosing class moved.  ``QuadOrchestrator`` keeps each
method with its original signature, so ``start()`` is untouched.

These belong together because they are one ordered sequence driven entirely by
``start()``: config -> database -> exchange -> market data -> risk -> execution
-> strategies -> optimizer -> Telegram -> health -> metrics -> AI.  Splitting
them across modules would scatter a strictly-ordered startup path and make the
dependencies between steps much harder to see, not easier.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import structlog

from quad.config.manager import ConfigManager
from quad.config.schema import AiConfig, QuadConfig
from quad.exchange.factory import create_exchange
from quad.execution.engine import ExecutionEngine
from quad.market_data.engine import MarketDataEngine
from quad.monitoring.error_sink import ErrorLogSink
from quad.persistence import create_database
from quad.risk.manager import RiskManager
from quad.strategy.factory import create_default_strategies

#: The only two environments the orchestrator will start in.  Anything else is
#: a configuration error and is rejected at startup rather than silently
#: trading live.
_VALID_MODES: frozenset[str] = frozenset({"bybit", "dry_run"})


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


def _install_error_sink_processor(sink: Any) -> None:
    """Insert *sink* into the live structlog processor chain.

    The sink must run *after* ``add_log_level`` (it reads the ``level`` key)
    and *before* the renderer (which consumes/collapses the event dict), so
    it is inserted immediately ahead of the last processor.  Idempotent: a
    second call replaces the previous sink rather than stacking two writers
    against the same table.
    """
    try:
        current = structlog.get_config()
        processors: list[Any] = list(current.get("processors") or [])
        # Drop any previously installed ErrorLogSink.
        processors = [p for p in processors if not isinstance(p, ErrorLogSink)]
        if processors:
            processors.insert(max(len(processors) - 1, 0), sink)
        else:  # pragma: no cover - structlog always has defaults
            processors = [sink]
        structlog.configure(
            processors=processors,
            **{k: v for k, v in current.items() if k != "processors"},
        )
    except Exception:
        # Logging configuration must never block bot startup.
        structlog.get_logger(__name__).warning(
            "error_sink_processor_install_failed", exc_info=True
        )


class BootstrapMixin:
    """One method per subsystem, called in dependency order by ``start()``.

    The attributes below are the state this mixin produces and consumes.  They
    are declared rather than assigned here, so the mixin documents the contract
    without owning it -- ``QuadOrchestrator.__init__`` remains the single place
    the state is created, and these methods populate it.
    """

    _log: Any
    _config_path: Any
    _config_dict: Any
    _config_manager: Any
    _mode: Any
    _cycle_interval: Any
    _ai_enabled: Any
    _ai_cycle_interval: Any
    _active_strategies: Any
    _db_manager: Any
    _error_sink: Any
    _exchange_adapter: Any
    _market_data: Any
    _risk_manager: Any
    _execution_engine: Any
    _optimizer: Any
    _bot: Any
    _telegram_bot: Any
    _telegram_chat_id: Any
    _health_server: Any
    _metrics: Any
    _groq_client: Any
    _tv_webhook: Any

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
        env-var scanning (which only covers ``QUAD_*`` and ``BYBIT_*``
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
        # Precedence: config file < QUAD_DSN / DATABASE_URL env < runtime set().
        # Both env vars are in ConfigManager.ENV_VAR_MAP, so the manager owns
        # this resolution; reading DATABASE_URL here too would create a second
        # source of truth that can disagree with the manager.
        dsn = config_manager.get(
            "persistence.dsn",
            self._config_dict["persistence"]["dsn"],
        )

        self._db_manager = create_database(
            dsn=str(dsn),
            min_pool_size=1,
            max_pool_size=5,
        )
        await self._db_manager.connect()
        await self._db_manager.initialize()
        await self._db_manager.migrate()
        self._log.info("database_initialized", dsn=self._db_manager.dsn)

        # Persist ERROR+ log events to the error_logs table.  The table and
        # its readers existed with no writer, so every recorded failure was
        # lost as soon as stdout rotated.  Wired here because the database
        # must be connected first.
        await self._init_error_sink()

    async def _init_error_sink(self) -> None:
        """Attach the structlog error sink and start its background flusher."""
        from quad.monitoring.error_sink import ErrorLogSink

        sink_cfg = self._config_dict.get("error_sink", {}) or {}
        if sink_cfg.get("enabled") is False:
            self._log.info("error_sink_disabled_config")
            self._error_sink = None
            return

        sink = ErrorLogSink(self._db_manager, self._config_dict)
        _install_error_sink_processor(sink)
        await sink.start()
        self._error_sink = sink
        self._log.info("error_sink_initialized", min_level=sink._min_level)

    async def _init_exchange_adapter(self) -> None:
        """Create and connect the exchange adapter.

        Maps ``QUAD_MODE`` to the exchange implementation:
            - ``"dry_run"`` -> Bybit with testnet=True
            - ``"bybit"`` -> Bybit (testnet or live per config)

        An unrecognised mode is a hard error.  It used to fall through
        silently, so a stale ``QUAD_MODE`` (e.g. a leftover ``okx``) left the
        bot trading Bybit while ``quad status`` advertised a different
        exchange than the one in use.
        """
        # Override exchange name based on mode
        mode = self._mode
        if mode not in _VALID_MODES:
            raise ValueError(
                f"Unknown _mode / QUAD_MODE {mode!r}. "
                f"Expected one of: {', '.join(sorted(_VALID_MODES))}."
            )
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

        Per-symbol failures are collected.  If a symbol ends up on a
        different leverage than requested the bot is trading on numbers the
        exchange never accepted, so the mismatch is recorded and surfaced
        (and fails startup in live, non-dry-run mode) rather than being
        silently logged away.
        """
        trading_cfg = self._config_dict.get("trading", {})
        leverage = int(trading_cfg.get("leverage", 1))
        margin_mode = str(trading_cfg.get("margin_mode", "isolated"))
        position_mode = str(trading_cfg.get("position_mode", "one_way"))
        symbols = list(trading_cfg.get("underlyings", []))
        adapter = self._exchange_adapter
        if adapter is None:
            return

        # Clamp the configured leverage to the risk ceiling so the value the
        # bot *sends* and the value it *prices with* are the same number.
        # Previously trading.leverage (10) and risk.max_leverage (50) could
        # disagree, and the bot sized brackets/liquidation distance from the
        # config value regardless of what the exchange accepted.
        risk_cfg = self._config_dict.get("risk", {}) or {}
        max_leverage = int(risk_cfg.get("max_leverage", leverage) or leverage)
        if leverage > max_leverage:
            self._log.warning(
                "account_setup_leverage_clamped",
                configured=leverage,
                max_leverage=max_leverage,
                msg=(
                    "trading.leverage exceeds risk.max_leverage; using the "
                    "lower value so sizing and the exchange agree."
                ),
            )
            leverage = max_leverage

        failed_symbols: list[str] = []
        leverage_mismatch: list[dict[str, Any]] = []

        for symbol in symbols:
            try:
                await adapter.set_leverage(symbol, leverage)
                self._log.info(
                    "account_setup_leverage_set",
                    symbol=symbol,
                    leverage=leverage,
                )
            except Exception as exc:
                failed_symbols.append(symbol)
                self._log.warning(
                    "account_setup_leverage_failed",
                    symbol=symbol,
                    leverage=leverage,
                    error=str(exc),
                )
            try:
                await adapter.set_margin_mode(symbol, margin_mode, leverage)
                self._log.info(
                    "account_setup_margin_mode_set",
                    symbol=symbol,
                    margin_mode=margin_mode,
                )
            except Exception as exc:
                if self._exchange_adapter.is_margin_mode_already_set(exc):
                    # Bybit 110043 "Margin mode is not modified" (handled
                    # by the adapter's is_margin_mode_already_set() method).
                    # symbol is already in the requested margin mode, so the
                    # call is a benign no-op.  Log at info, not a warning.
                    self._log.info(
                        "account_setup_margin_mode_already",
                        symbol=symbol,
                        margin_mode=margin_mode,
                    )
                else:
                    failed_symbols.append(symbol)
                    self._log.warning(
                        "account_setup_margin_mode_failed",
                        symbol=symbol,
                        error=str(exc),
                    )

        # Read back what the exchange actually has.  A set_leverage() that
        # silently no-ops (open position, risk-limit tier) leaves the account
        # on a different leverage than every downstream calculation assumes.
        try:
            positions = await adapter.get_positions()
            for pos in positions:
                sym = str(
                    getattr(pos, "symbol", "") or getattr(pos, "contract_symbol", "")
                )
                if sym not in symbols:
                    continue
                actual = getattr(pos, "leverage", None)
                if actual is None:
                    continue
                try:
                    actual_i = int(actual)
                except (TypeError, ValueError):
                    continue
                if actual_i != leverage:
                    leverage_mismatch.append(
                        {"symbol": sym, "requested": leverage, "exchange": actual_i}
                    )
        except Exception as exc:
            self._log.debug("account_setup_leverage_readback_failed", error=str(exc))

        if leverage_mismatch:
            self._log.error(
                "account_setup_leverage_mismatch",
                mismatches=leverage_mismatch,
                msg=(
                    "The exchange is running a different leverage than the bot "
                    "is configured for.  Sizing, bracket placement and "
                    "liquidation distance all assume the configured value."
                ),
            )
        if failed_symbols:
            self._log.error(
                "account_setup_incomplete",
                failed_symbols=sorted(set(failed_symbols)),
                msg=(
                    "One or more per-symbol account settings were not applied. "
                    "Those symbols will trade with the exchange's existing "
                    "settings, not the configured ones."
                ),
            )
        # Live trading must not proceed on unverified account settings.
        if (failed_symbols or leverage_mismatch) and not self._is_dry_run:
            raise RuntimeError(
                "futures account setup incomplete: "
                f"failed={sorted(set(failed_symbols))} "
                f"mismatch={leverage_mismatch}. Refusing to trade live with "
                "unverified leverage/margin settings."
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
                make_repo,
            )

            # Validate config as QuadConfig (Pydantic model needed by Optimizer)
            config = QuadConfig.model_validate(self._config_dict)

            db = self._db_manager

            self._optimizer = Optimizer(
                config=config,
                groq_client=self._groq_client,
                decision_repo=make_repo(DecisionRepository, db, self._config_dict),
                trade_repo=make_repo(TradeRepository, db, self._config_dict),
                performance_repo=make_repo(
                    PerformanceSnapshotRepository, db, self._config_dict
                ),
                run_repo=make_repo(OptimizationRunRepository, db, self._config_dict),
                recommendation_repo=make_repo(
                    OptimizationRecommendationRepository, db, self._config_dict
                ),
                config_change_repo=make_repo(
                    ConfigChangeRepository, db, self._config_dict
                ),
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
        port = int(
            os.environ.get("QUAD_HEALTH_PORT", "")
            or health_cfg.get("port", 9090)
            or 9090
        )
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

        The health server is started earlier (it must answer liveness probes
        even when later init steps are slow), so it was constructed with
        ``metrics_collector=None``.  Attach the collector here so ``/metrics``
        serves real gauges instead of the uptime-only fallback.
        """
        from quad.monitoring.metrics import MetricsCollector

        self._metrics = MetricsCollector()
        self._metrics.set_gauge("orchestrator_started", 1.0)
        self._metrics.set_gauge("dry_run", 1.0 if self._is_dry_run else 0.0)
        if self._health_server is not None:
            try:
                self._health_server.set_metrics_collector(self._metrics)
            except Exception as exc:
                self._log.warning(
                    "health_metrics_attach_failed",
                    error=str(exc),
                )
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
