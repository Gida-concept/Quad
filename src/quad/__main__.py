"""Entry point for running quad as a module: ``python -m quad``

Creates and runs the ``QuadOrchestrator`` with graceful shutdown
handling.  All logging is configured before the orchestrator starts.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys

import structlog
from structlog.types import Processor

VERSION = "0.1.0"


class _TokenRedactionFilter(logging.Filter):
    """Redact Telegram bot tokens from emitted log records.

    python-telegram-bot's httpx/httpcore transport logs the full request
    URL at INFO, which includes the bot token in the path
    (``/bot<token>/<method>``).  This filter scrubs any ``/bot<id>:<secret>``
    pattern from the formatted message before it reaches stdout.
    """

    _TOKEN_RE = re.compile(r"/bot\d{5,}:[A-Za-z0-9_-]{20,}")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            if "bot" in message and self._TOKEN_RE.search(message):
                record.msg = self._TOKEN_RE.sub("/bot***REDACTED***", message)
                record.args = ()
        except Exception:  # noqa: S110
            # Redaction must never break logging.
            pass
        return True


def _configure_logging() -> None:
    """Configure structlog for production logging.

    Uses JSON format by default (configurable via ``QUAD_LOG_FORMAT``).
    Log level is set from ``QUAD_LOG_LEVEL`` (default ``INFO``).

    On Windows, stdout/stderr are reconfigured to UTF-8 to work around
    ConsoleRenderer emitting Unicode chars (box drawing, emoji) that
    crash on cp1252.
    """
    log_level = os.environ.get("QUAD_LOG_LEVEL", "INFO").upper()
    log_format = os.environ.get("QUAD_LOG_FORMAT", "json").lower()

    # Windows console encoding workaround
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
    processors: list[Processor] = [
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if log_format == "console":
        processors.append(structlog.dev.ConsoleRenderer())
    else:
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Silent HTTP client loggers that emit full request URLs at INFO.
    # httpx/httpcore (used by python-telegram-bot and the Groq SDK) log
    # ``HTTP Request: POST https://api.telegram.org/bot<token>/...`` which
    # leaks the bot token into every log line.  Keep them at WARNING+; the
    # redaction filter below is defense in depth for any other logger.
    for _noisy_logger in ("httpx", "httpcore"):
        logging.getLogger(_noisy_logger).setLevel(logging.WARNING)

    # Explicitly attach a StreamHandler to the root logger so INFO+ records
    # reach stdout. structlog's stdlib LoggerFactory delegates to the stdlib
    # ``logging`` module, but without a handler stdlib only has the
    # `lastResort` handler, which emits WARNING+ to stderr — every INFO record
    # is dropped. The app logs mostly at INFO, so nothing appeared in the
    # Docker / Dokploy logs. A stdout StreamHandler is captured by the
    # container's json-file logging driver, matching QUAD_LOG_LEVEL.
    #
    # structlog's stdlib BoundLogger renders the full event (processors +
    # JSONRenderer) and passes it to the stdlib logger as the formatted
    # message string, so the handler only needs to dump that message verbatim.
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setLevel(log_level)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(_TokenRedactionFilter())
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.addHandler(handler)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for ``python -m quad``."""
    parser = argparse.ArgumentParser(
        prog="quad",
        description="Quad USD-M Futures trading bot.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"quad {VERSION}",
        help="Print the version and exit.",
    )
    parser.add_argument(
        "--config",
        "-c",
        default=None,
        metavar="PATH",
        help="Path to config YAML (overrides QUAD_CONFIG_PATH env var).",
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> None:
    """Application entry point."""
    args = _parse_args(argv)

    _configure_logging()
    log = structlog.get_logger()

    log.info("quad_starting", version=VERSION)

    config_path = args.config or os.environ.get(
        "QUAD_CONFIG_PATH",
        "config/config.yaml",
    )

    from quad.orchestrator import QuadOrchestrator

    orchestrator = QuadOrchestrator(config_path=config_path)
    await orchestrator.run_forever()

    log.info("quad_stopped", version=VERSION)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Graceful exit -- orchestrator handles cleanup in run_forever()
        pass
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(1)
