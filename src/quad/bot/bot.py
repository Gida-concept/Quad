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
import datetime as dt
from typing import Any

import structlog
from telegram import Update
from telegram.error import Conflict, TelegramError
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
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

        # Startup probe: check if another instance is already polling this bot token.
        # A Conflict here means another process is polling — we log a warning
        # and wait a few seconds before our own polling attempt.
        try:
            await self._application.bot.get_updates(
                offset=-1,
                timeout=1,
                read_timeout=2,
            )
        except Conflict:
            self._log.warning(
                "polling_conflict_detected_on_probe",
                msg=(
                    "Another Telegram bot instance appears to be polling "
                    "this bot token. Waiting 5 seconds before attempting "
                    "to start polling."
                ),
            )
            await asyncio.sleep(5)
        except TelegramError:
            # Non-conflict Telegram errors during the probe are non-fatal;
            # they may indicate a brief network hiccup — proceed anyway.
            pass

        max_polling_retries = 3
        polling_retry_delay = 2.0
        polling_started = False

        for polling_attempt in range(max_polling_retries):
            try:
                await self._application.updater.start_polling(  # type: ignore[union-attr]
                    drop_pending_updates=True,
                )
                polling_started = True
                break
            except Conflict as exc:
                wait = polling_retry_delay * (2**polling_attempt)
                self._log.warning(
                    "polling_conflict_retrying",
                    attempt=polling_attempt + 1,
                    max_retries=max_polling_retries,
                    wait_s=wait,
                    error=str(exc),
                )
                if polling_attempt < max_polling_retries - 1:
                    await asyncio.sleep(wait)
            except TelegramError as exc:
                if "Conflict" in str(exc):
                    wait = polling_retry_delay * (2**polling_attempt)
                    self._log.warning(
                        "polling_conflict_retrying",
                        attempt=polling_attempt + 1,
                        max_retries=max_polling_retries,
                        wait_s=wait,
                        error=str(exc),
                    )
                    if polling_attempt < max_polling_retries - 1:
                        await asyncio.sleep(wait)
                else:
                    self._log.warning(
                        "polling_start_failed_non_conflict",
                        error=str(exc),
                    )
                    break

        if polling_started:
            self._log.info("bot_started")
        else:
            self._log.warning(
                "bot_started_without_polling",
                msg=(
                    "Telegram bot application is running but polling could "
                    "not start due to a persistent Conflict error. The bot "
                    "will still send notifications and run background jobs, "
                    "but will not receive commands until the conflicting "
                    "instance is stopped and polling is re-established."
                ),
            )

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

    def _rate_limit_wrapper(
        self,
        cmd: str,
        handler,
    ):
        """Wrap a command handler with per-user rate limiting.

        Returns an async callback suitable for ``CommandHandler`` that
        checks the rate limit before delegating to *handler*.
        """

        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
            user_id = update.effective_user.id if update.effective_user else 0
            remaining = self._commands._check_rate_limit(user_id, cmd)
            if remaining is not None:
                if update.message:
                    await update.message.reply_text(
                        f"Please wait {remaining}s before using /{cmd} again.",
                    )
                return
            return await handler(update, context)

        return wrapped

    def _setup_handlers(self) -> None:
        """Register all CommandHandlers and the error handler."""
        if self._application is None:
            return

        app = self._application

        # Simple command handlers (rate limited)
        _command_names = [
            "start",
            "help",
            "status",
            "balance",
            "positions",
            "orders",
            "funding_rate",
            "book",
            "leverage",
            "position_mode",
            "liquidation_warnings",
            "market_regime",
            "strategies",
            "kill",
            "risk",
            "cancel",
            "settings",
            "set",
            "analyze",
            "ai_strategy",
            "ai_status",
            "ai_decision",
        ]
        for name in _command_names:
            handler = getattr(self._commands, f"cmd_{name}", None)
            if handler is not None:
                app.add_handler(
                    CommandHandler(name, self._rate_limit_wrapper(name, handler))
                )

        # Execute conversation handler (multi-step, not rate-limited)
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
