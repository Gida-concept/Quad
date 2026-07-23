"""Quad Telegram bot — main application class.

Built on python-telegram-bot v20+ ``Application`` pattern.
Uses async polling (no webhook) for personal deployment.

Exports
-------
QuadBot
    The main Telegram bot application.
"""

from __future__ import annotations

from typing import Any

import structlog
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
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
    ) -> None:
        self._log = logger.bind()
        self._config = config
        self._telegram_config = config.get("telegram", {})

        # Store component references for command handlers
        self._orchestrator = orchestrator
        self._risk_manager = risk_manager
        self._execution_engine = execution_engine
        self._market_data_engine = market_data_engine
        self._db_manager = db_manager
        self._groq_client = groq_client

        # Bot token and admin verification
        self._bot_token: str = self._telegram_config.get("bot_token", "")
        self._admin_ids: list[int] = self._telegram_config.get("admin_ids", [])
        self._notification_chat_id: int | None = self._telegram_config.get(
            "notification_chat_id", None
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
            "admin_ids": self._admin_ids,
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
                "Set config['telegram']['bot_token'] or the QUAD_TELEGRAM_BOT_TOKEN "
                "environment variable."
            )
            self._log.error("bot_token_missing")
            raise ValueError(msg)

        self._log.info(
            "bot_starting",
            admin_ids=self._admin_ids,
            notification_chat_id=self._notification_chat_id,
        )

        # Build the application
        app_builder: ApplicationBuilder = (
            Application.builder()
            .token(self._bot_token)
            .concurrent_updates(True)
        )
        self._application = app_builder.build()

        # Register handlers
        self._setup_handlers()

        # Register jobs
        self._setup_jobs()

        # Start polling
        await self._application.initialize()
        await self._application.start()
        await self._application.updater.start_polling()  # type: ignore[union-attr]

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
        app.add_handler(CommandHandler("chain", self._commands.cmd_chain))
        app.add_handler(CommandHandler("strategies", self._commands.cmd_strategies))
        app.add_handler(CommandHandler("kill", self._commands.cmd_kill))
        app.add_handler(CommandHandler("risk", self._commands.cmd_risk))
        app.add_handler(CommandHandler("cancel", self._commands.cmd_cancel))
        app.add_handler(CommandHandler("settings", self._commands.cmd_settings))

        # AI-powered commands
        app.add_handler(CommandHandler("analyze", self._commands.cmd_analyze))
        app.add_handler(CommandHandler("ai_strategy", self._commands.cmd_ai_strategy))
        app.add_handler(CommandHandler("ai_status", self._commands.cmd_ai_status))
        app.add_handler(CommandHandler("ai_decision", self._commands.cmd_ai_decision))

        # Execute conversation handler (multi-step)
        app.add_handler(self._commands.get_execute_conversation_handler())

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

        # Status summary: every 60 minutes
        job_queue.run_repeating(
            self._jobs.job_status_summary,
            interval=3600,
            first=60,
            name="status_summary",
        )

        # Risk alert check: every 5 minutes
        job_queue.run_repeating(
            self._jobs.job_risk_alert,
            interval=300,
            first=120,
            name="risk_alert",
        )

        # Daily report: scheduled at configured time (default 23:00 UTC)
        daily_hour = self._telegram_config.get("daily_report_hour", 23)
        daily_minute = self._telegram_config.get("daily_report_minute", 0)

        import datetime as dt

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

        self._log.debug(
            "jobs_registered",
            status_summary_interval_s=3600,
            risk_alert_interval_s=300,
            daily_report_hour=daily_hour,
            daily_report_minute=daily_minute,
        )
