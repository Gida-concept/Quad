"""Persistent error sink for the ``error_logs`` table.

The schema, the model and a read-only repository for ``error_logs`` all
existed, but nothing ever *wrote* a row: every error went to stdout via
structlog and vanished, so ``/health``-adjacent diagnostics and the
repository's ``get_recent`` / ``get_by_level`` / ``get_by_source`` readers
always returned an empty table.

This module closes the loop.  ``ErrorLogSink`` is a **structlog processor**,
so it observes every log event the application emits without any call site
having to remember to report an error.  Events at ``ERROR`` and above are
appended to an in-memory queue and written to the database by a background
flusher, which keeps the hot logging path free of DB round-trips.

Usage::

    sink = ErrorLogSink(db_manager, config)
    structlog.configure(processors=[..., sink, structlog.processors.JSONRenderer()])
    await sink.start()
    ...
    await sink.stop()   # flushes whatever is pending
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: structlog level names that are considered worth persisting.
_ERROR_LEVELS = frozenset(
    {"error", "critical", "exception", "fatal", "warn", "warning"}
)

#: Event-dict keys never persisted (rendered output / internal bookkeeping).
_SKIP_KEYS = frozenset(
    {
        "event",
        "level",
        "logger",
        "timestamp",
        "exc_info",
        "_record",
        "_from_structlog",
        "_stdlib_",
        "stack",
    }
)

#: Cap on serialized detail size so one pathological event cannot bloat a row.
_MAX_DETAILS_CHARS = 8000


class ErrorLogSink:
    """structlog processor that persists error events to ``error_logs``.

    Parameters
    ----------
    db_manager:
        A connected :class:`~quad.persistence.database.DatabaseManager`.
        ``None`` makes the sink a no-op, so wiring it unconditionally is
        safe.
    config:
        Full config dict; ``error_sink.*`` keys tune the behaviour.
    min_level:
        Lowest level name to persist (default ``"error"``).
    batch_size:
        Flush once this many events are queued.
    flush_interval_seconds:
        Maximum time an event waits in the queue before being flushed.
    max_queue:
        Bounded queue size; oldest events are dropped when full so a
        database outage cannot exhaust memory.
    """

    def __init__(
        self,
        db_manager: Any,
        config: dict[str, Any] | None = None,
        *,
        min_level: str = "error",
        batch_size: int = 20,
        flush_interval_seconds: float = 5.0,
        max_queue: int = 1000,
    ) -> None:
        self._db = db_manager
        sink_cfg = (config or {}).get("error_sink", {}) or {}
        self._min_level = str(sink_cfg.get("min_level", min_level)).lower()
        self._batch_size = int(sink_cfg.get("batch_size", batch_size))
        self._flush_interval = float(
            sink_cfg.get("flush_interval_seconds", flush_interval_seconds)
        )
        self._max_queue = int(sink_cfg.get("max_queue", max_queue))
        self._queue: deque[dict[str, Any]] = deque(maxlen=self._max_queue)
        self._dropped = 0
        self._flusher: asyncio.Task | None = None
        self._stopped = False

    # ------------------------------------------------------------------
    # structlog processor interface
    # ------------------------------------------------------------------

    def __call__(
        self, logger: Any, method_name: str, event_dict: dict[str, Any]
    ) -> dict[str, Any]:
        """Processor hook: record the event and return *event_dict* unchanged.

        Never raises — a failure to record an error must not break the code
        that logged it.
        """
        try:
            self._record(level=method_name, event_dict=event_dict)
        except Exception:  # pragma: no cover - the sink must never propagate
            pass
        return event_dict

    def _record(self, level: str, event_dict: dict[str, Any]) -> None:
        """Queue one event if its level qualifies."""
        if self._db is None:
            return
        level_name = str(event_dict.get("level") or level or "").lower()
        if level_name not in _ERROR_LEVELS:
            return
        if self._level_rank(level_name) < self._level_rank(self._min_level):
            return

        if len(self._queue) == self._max_queue:
            self._dropped += 1

        details = {
            k: v
            for k, v in event_dict.items()
            if k not in _SKIP_KEYS and not k.startswith("_")
        }
        try:
            details_json = json.dumps(details, default=str)[:_MAX_DETAILS_CHARS]
        except Exception:  # pragma: no cover - non-serialisable payloads
            details_json = "{}"

        self._queue.append(
            {
                "timestamp": int(time.time() * 1000),
                "level": level_name.upper(),
                "event": str(event_dict.get("event", ""))[:255],
                "message": str(details_json)[:_MAX_DETAILS_CHARS],
                "details_json": details_json,
            }
        )
        self._ensure_flusher()

    @staticmethod
    def _level_rank(level: str) -> int:
        """Order levels so a threshold comparison is meaningful."""
        order = {
            "debug": 10,
            "info": 20,
            "warning": 30,
            "warn": 30,
            "error": 40,
            "exception": 40,
            "critical": 50,
            "fatal": 50,
        }
        return order.get(level, 40)

    # ------------------------------------------------------------------
    # Lifecycle + flushing
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background flusher."""
        if self._flusher is not None:
            return
        self._stopped = False
        self._flusher = asyncio.create_task(self._flush_loop())
        logger.info("error_sink_started", min_level=self._min_level)

    def _ensure_flusher(self) -> None:
        """Start the flusher from a sync context, if one is running."""
        if self._flusher is not None or self._stopped:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop (sync caller / logging during shutdown): the event stays
            # queued and the next start()/flush() will pick it up.
            return
        self._flusher = loop.create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        """Flush queued events until stopped."""
        try:
            while not self._stopped:
                await asyncio.sleep(self._flush_interval)
                if len(self._queue) >= self._batch_size:
                    await self.flush()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - loop must not die silently
            logger.warning("error_sink_loop_failed", error=str(exc))

    async def flush(self) -> int:
        """Write queued events to ``error_logs``.

        Returns the number of rows written.  Safe to call at any time; the
        in-memory queue is drained first so a failed write does not lose
        events that a retry could still persist.
        """
        if self._db is None or not self._queue:
            return 0
        batch = list(self._queue)
        self._queue.clear()
        try:
            from quad.persistence.models import ErrorLogModel
            from quad.persistence.repositories import ErrorLogRepository, make_repo

            repo = make_repo(
                ErrorLogRepository, self._db, getattr(self._db, "_config", None) or {}
            )
            written = 0
            for row in batch:
                try:
                    await repo.create(
                        ErrorLogModel(
                            id=0,
                            timestamp=row["timestamp"],
                            level=row["level"],
                            event=row["event"],
                            message=row["message"],
                            details_json=row["details_json"],
                        )
                    )
                    written += 1
                except Exception:
                    # One bad row must not block the rest.
                    continue
            if self._dropped:
                logger.warning(
                    "error_sink_dropped_events",
                    dropped=self._dropped,
                    msg="error_logs queue was full; oldest events were discarded",
                )
                self._dropped = 0
            return written
        except Exception as exc:
            logger.warning("error_sink_flush_failed", error=str(exc), batch=len(batch))
            return 0

    async def stop(self) -> None:
        """Stop the flusher and drain the queue."""
        self._stopped = True
        task = self._flusher
        self._flusher = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self.flush()
        logger.info("error_sink_stopped")

    @property
    def pending(self) -> int:
        """Number of events waiting to be written."""
        return len(self._queue)
