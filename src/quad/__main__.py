"""Entry point for running quad as a module: ``python -m quad``

Creates and runs the ``QuadOrchestrator`` with graceful shutdown
handling.  All logging is configured before the orchestrator starts.
"""

from __future__ import annotations

import asyncio
import os
import sys

import structlog

VERSION = "0.1.0"


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
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    processors = [
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


async def main() -> None:
    """Application entry point."""
    _configure_logging()
    log = structlog.get_logger()

    log.info("quad_starting", version=VERSION)

    config_path = os.environ.get(
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
    except Exception:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        sys.exit(1)
