"""Quad Telegram bot — main application class.

Built on python-telegram-bot v20+ ``Application`` pattern.
Uses async polling (no webhook) for personal deployment.

Exports
-------
QuadBot
    The main Telegram bot application.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from telegram.error import TelegramError
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .commands import QuadBotCommands
from .jobs import QuadBotJobs

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = structlog.get_logger(__name__)


# ============================================================================
# QuadBot
# ============================================================================


class QuadBot:
    """Quad Telegram bot — PRIMARY user interface.

    Uses the python-telegram-bot v20+ ``Application`` pattern with
    async polling.  All configuration (bot token, admin IDs, notification
    chat ID) is loaded from the project config dictionary.

    Parameters
    ----------
    config:
        Full configuration dictionary.  The ``telegram`` subsection is
        extracted automatically.
    orchestrator:
        The top-level orchestrator (fully typed in Phase 10).  Used for
        status, balance, position, and order queries.
    risk_manager:
        Optional risk manager for risk-related queries and kill switch.
    execution_engine:
        Optional execution engine for order management.
    market_data_engine:
        Optional market data engine for option chain queries.
    db_manager:
        Optional database manager for persistence queries.
    optimizer:
        Optional strategy self-optimizer for auto-retrain cycles.
    """

    def __init__(
        self,
        config: dict[str, Any],
        orchestrator: Any = None,
        risk_manager: Any = None,
        execution_engine: Any = None,
        market_data_engine: Any = None,
        db_manager: Any = None,
        groq_client: Any = None,
        optimizer: Any = None,
    ) -> None:
        self._log = logger.bind()
        self._config = config
        self._telegram_config = config["telegram"]

        # Store component references for command handlers
        self._orchestrator = orchestrator
        self._risk_manager = risk_manager
        self._execution_engine = execution_engine
        self._market_data_engine = market_data_engine
        self._db_manager = db_manager
        self._groq_client = groq_client
        self._optimizer = optimizer

        # Bot token and notification config
        self._bot_token: str = self._telegram_config["bot_token"]
        self._notification_chat_id: int | None = self._telegram_config.get(
            "notification_chat_id"
        )

        # Build shared state for command / job handlers
        self._shared_state: dict[str, Any] = {
            "config": config,
            "telegram_config": self._telegram_config,
            "orchestrator": orchestrator,
            "risk_manager": risk_manager,
            "execution_engine": execution_engine,
            "market_data_engine": market_data_engine,
            "db_manager": db_manager,
            "groq_client": groq_client,
            "optimizer": optimizer,
            "notification_chat_id": self._notification_chat_id,
        }

        # Application (created in start())
        self._application: Application | None = None

        # Commands and jobs
        self._commands = QuadBotCommands(self._shared_state)
        self._jobs = QuadBotJobs(self._shared_state)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialize the Application, register handlers, and start polling.

        Raises
        ------
        ValueError
            If ``telegram.bot_token`` is not configured.
        """
        if not self._bot_token:
            msg = (
                "Telegram bot token is not configured. "
                "Set config['telegram']['bot_token'] or the TELEGRAM_BOT_TOKEN "
                "environment variable."
            )
            self._log.error("bot_token_missing")
            raise ValueError(msg)

        self._log.info(
            "bot_starting",
            notification_chat_id=self._notification_chat_id,
        )

        # Build the application
        request = HTTPXRequest(connect_timeout=10, read_timeout=30)
        app_builder: ApplicationBuilder = (
            Application.builder()
            .token(self._bot_token)
            .request(request)
            .concurrent_updates(True)
        )
        self._application = app_builder.build()

        # Register handlers
        self._setup_handlers()

        # Register jobs
        self._setup_jobs()

        # Start polling with retry for "Conflict: terminated by other getUpdates request"
        # Telegram Bot API only allows ONE long-polling getUpdates connection per token.
        # When a Docker container restarts, the old connection may still be active
        # on Telegram's server for a few seconds, causing this conflict.
        # The retry with exponential backoff lets the old connection time out.
        await self._application.initialize()
        await self._application.start()
        max_polling_retries = 3
        polling_retry_delay = 2.0

        for polling_attempt in range(max_polling_retries):
            try:
                await self._application.updater.start_polling(  # type: ignore[union-attr]
                    drop_pending_updates=True,
                )
                break
            except TelegramError as exc:
                if "Conflict" in str(exc) and polling_attempt < max_polling_retries - 1:
                    wait = polling_retry_delay * (2**polling_attempt)
                    self._log.warning(
                        "polling_conflict_retrying",
                        attempt=polling_attempt + 1,
                        wait_s=wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                raise

        self._log.info("bot_started")

    async def stop(self) -> None:
        """Gracefully shut down the Application."""
        if self._application is None:
            self._log.warning("bot_not_running")
            return

        self._log.info("bot_stopping")
        try:
            await self._application.updater.stop()  # type: ignore[union-attr]
            await self._application.stop()
            await self._application.shutdown()
        except Exception as exc:
            self._log.exception("bot_stop_error", error=str(exc))
        self._application = None
        self._log.info("bot_stopped")

    # ------------------------------------------------------------------
    # Property access
    # ------------------------------------------------------------------

    @property
    def application(self) -> Application | None:
        """Return the underlying PTB Application, or ``None`` if not started."""
        return self._application

    @property
    def is_running(self) -> bool:
        """Return ``True`` if the bot application has been started."""
        return self._application is not None

    # ------------------------------------------------------------------
    # Internal: handler / job registration
    # ------------------------------------------------------------------

    def _setup_handlers(self) -> None:
        """Register all CommandHandlers and the error handler."""
        if self._application is None:
            return

        app = self._application

        # Simple command handlers
        app.add_handler(CommandHandler("start", self._commands.cmd_start))
        app.add_handler(CommandHandler("help", self._commands.cmd_help))
        app.add_handler(CommandHandler("status", self._commands.cmd_status))
        app.add_handler(CommandHandler("balance", self._commands.cmd_balance))
        app.add_handler(CommandHandler("positions", self._commands.cmd_positions))
        app.add_handler(CommandHandler("orders", self._commands.cmd_orders))
        app.add_handler(CommandHandler("funding_rate", self._commands.cmd_funding_rate))
        app.add_handler(CommandHandler("book", self._commands.cmd_book))
        app.add_handler(CommandHandler("leverage", self._commands.cmd_leverage))
        app.add_handler(CommandHandler("position_mode", self._commands.cmd_position_mode))
        app.add_handler(CommandHandler("liquidation_warnings", self._commands.cmd_liquidation_warnings))
        app.add_handler(CommandHandler("market_regime", self._commands.cmd_market_regime))
        app.add_handler(CommandHandler("strategies", self._commands.cmd_strategies))
        app.add_handler(CommandHandler("kill", self._commands.cmd_kill))
        app.add_handler(CommandHandler("risk", self._commands.cmd_risk))
        app.add_handler(CommandHandler("cancel", self._commands.cmd_cancel))
        app.add_handler(CommandHandler("settings", self._commands.cmd_settings))
        app.add_handler(CommandHandler("set", self._commands.cmd_set))

        # AI-powered commands
        app.add_handler(CommandHandler("analyze", self._commands.cmd_analyze))
        app.add_handler(CommandHandler("ai_strategy", self._commands.cmd_ai_strategy))
        app.add_handler(CommandHandler("ai_status", self._commands.cmd_ai_status))
        app.add_handler(CommandHandler("ai_decision", self._commands.cmd_ai_decision))

        # Execute conversation handler (multi-step)
        app.add_handler(self._commands.get_execute_conversation_handler())

        # Kill switch inline keyboard callback handler
        app.add_handler(
            CallbackQueryHandler(self._commands.cmd_kill_callback, pattern=r"^kill_")
        )

        # Error handler
        app.add_error_handler(self._commands.error_handler)

        self._log.debug("handlers_registered")

    def _setup_jobs(self) -> None:
        """Register recurring jobs in the PTB job queue.

        Jobs run only if a ``notification_chat_id`` is configured.
        """
        if self._application is None:
            return

        job_queue = self._application.job_queue
        if job_queue is None:
            self._log.warning("job_queue_not_available")
            return

        import datetime as dt

        self._job_intervals = self._telegram_config["job_intervals"]

        # Status summary: configurable interval
        status_interval = self._job_intervals["status_summary_seconds"]
        status_first = self._job_intervals.get("status_summary_first_seconds")
        job_queue.run_repeating(
            self._jobs.job_status_summary,
            interval=status_interval,
            first=status_first,
            name="status_summary",
        )

        # Risk alert check: configurable interval
        risk_alert_interval = self._job_intervals["risk_alert_seconds"]
        risk_alert_first = self._job_intervals.get("risk_alert_first_seconds")
        job_queue.run_repeating(
            self._jobs.job_risk_alert,
            interval=risk_alert_interval,
            first=risk_alert_first,
            name="risk_alert",
        )

        # Daily report: scheduled at configured time
        daily_report_cfg = self._telegram_config["daily_report"]
        daily_hour = daily_report_cfg["hour"]
        daily_minute = daily_report_cfg["minute"]

        now = dt.datetime.now(dt.timezone.utc)
        first_daily = now.replace(
            hour=daily_hour,
            minute=daily_minute,
            second=0,
            microsecond=0,
        )
        if first_daily <= now:
            first_daily += dt.timedelta(days=1)

        job_queue.run_daily(
            self._jobs.job_daily_report,
            time=dt.time(hour=daily_hour, minute=daily_minute, tzinfo=dt.timezone.utc),
            name="daily_report",
        )

        # Optimization cycle: configurable interval (default every 7 days)
        if self._optimizer is not None:
            interval_days = int(
                self._config["retrain"]["interval_days"]
            )
            interval_s = interval_days * 86400
            first_s = int(
                self._config["retrain"]["initial_delay_hours"]
            ) * 3600

            job_queue.run_repeating(
                self._jobs.job_optimization_cycle,
                interval=interval_s,
                first=first_s,
                name="optimization_cycle",
            )
            self._log.info(
                "optimization_job_registered",
                interval_days=interval_days,
                first_s=first_s,
            )

        # Funding rate countdown: configurable interval (default 30 minutes)
        funding_interval = self._job_intervals["funding_rate_countdown_seconds"]
        funding_first = self._job_intervals.get("funding_rate_countdown_first_seconds")
        job_queue.run_repeating(
            self._jobs.job_funding_rate_countdown,
            interval=funding_interval,
            first=funding_first,
            name="funding_rate_countdown",
        )

        # Liquidation warning: configurable interval (default 5 minutes)
        liq_interval = self._job_intervals["liquidation_warning_seconds"]
        liq_first = self._job_intervals.get("liquidation_warning_first_seconds")
        job_queue.run_repeating(
            self._jobs.job_liquidation_warning,
            interval=liq_interval,
            first=liq_first,
            name="liquidation_warning",
        )

        # Funding cost report: daily at configured time (default 22:00 UTC)
        funding_cost_cfg = self._telegram_config["funding_cost_report"]
        funding_cost_hour = funding_cost_cfg["hour"]
        funding_cost_minute = funding_cost_cfg["minute"]
        job_queue.run_daily(
            self._jobs.job_funding_cost_report,
            time=dt.time(hour=funding_cost_hour, minute=funding_cost_minute, tzinfo=dt.timezone.utc),
            name="funding_cost_report",
        )

        self._log.debug(
            "jobs_registered",
            status_summary_interval_s=status_interval,
            risk_alert_interval_s=risk_alert_interval,
            daily_report_hour=daily_hour,
            daily_report_minute=daily_minute,
            optimization_registered=self._optimizer is not None,
            funding_rate_countdown_interval_s=funding_interval,
            liquidation_warning_interval_s=liq_interval,
            funding_cost_report_hour=funding_cost_hour,
        )
