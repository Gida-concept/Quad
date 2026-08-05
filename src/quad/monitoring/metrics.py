"""Metrics collector for Quad monitoring.

Collects and exposes metrics from all subsystems as Prometheus-style
text.  Other subsystems push metrics here; ``HealthServer`` reads from
here.
"""

from __future__ import annotations

import threading
import time as _time
from collections import deque
from typing import Any

import structlog

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = structlog.get_logger(__name__)


# ============================================================================
# MetricsCollector
# ============================================================================


class MetricsCollector:
    """In-memory metrics collector.

    Acts as a central registry for gauge, counter, and histogram values.
    Other subsystems call ``set_gauge()``, ``increment_counter()``, and
    ``observe_histogram()`` to push data; the health server reads it via
    ``get_metrics_text()`` for Prometheus scraping.

    Thread-safe: all mutations are protected by a lock.
    """

    def __init__(self, max_observations: int = 1000) -> None:
        """Initialize the metrics collector.

        Parameters
        ----------
        max_observations:
            Maximum number of observations retained per histogram.  Older
            values are discarded when the limit is reached.  Default 1000.
        """
        self._lock = threading.Lock()
        self._max_observations = max_observations

        # Internal storage: {name: value}
        self._gauges: dict[str, float] = {}
        self._counters: dict[str, float] = {}
        self._histograms: dict[str, deque[float]] = {}

        # Label storage: {name: {label_key: value}}
        self._gauge_labels: dict[str, dict[str, str]] = {}
        self._counter_labels: dict[str, dict[str, str]] = {}
        self._histogram_labels: dict[str, dict[str, str]] = {}

        self._start_time = _time.time()

    # ------------------------------------------------------------------
    # Metric mutation
    # ------------------------------------------------------------------

    def set_gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        """Set a gauge value.

        Parameters
        ----------
        name:
            Metric name (e.g. ``"quad_active_positions"``).
        value:
            Current value.
        labels:
            Optional label dict (e.g. ``{"symbol": "BTCUSDT"}``).
        """
        with self._lock:
            self._gauges[name] = value
            if labels:
                self._gauge_labels[name] = labels

    def increment_counter(self, name: str, amount: float = 1.0, labels: dict[str, str] | None = None) -> None:
        """Increment a counter value.

        Parameters
        ----------
        name:
            Metric name (e.g. ``"quad_orders_submitted_total"``).
        amount:
            Amount to increment by (default 1.0).
        labels:
            Optional label dict.
        """
        with self._lock:
            self._counters[name] = self._counters.get(name, 0.0) + amount
            if labels:
                self._counter_labels[name] = labels

    def observe_histogram(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        """Record a histogram observation.

        Old observations beyond ``max_observations`` are automatically
        discarded to bound memory usage.

        Parameters
        ----------
        name:
            Metric name (e.g. ``"quad_order_latency_seconds"``).
        value:
            Observed value (converted to float).
        labels:
            Optional label dict.
        """
        with self._lock:
            if name not in self._histograms:
                self._histograms[name] = deque(maxlen=self._max_observations)
            self._histograms[name].append(float(value))
            if labels:
                self._histogram_labels[name] = labels

    # ------------------------------------------------------------------
    # Metric reading
    # ------------------------------------------------------------------

    def get_all(self) -> dict[str, Any]:
        """Get all current metrics as a dict.

        Returns
        -------
        dict
            Keys: ``"gauges"``, ``"counters"``, ``"histograms"``.
        """
        with self._lock:
            return {
                "gauges": dict(self._gauges),
                "counters": dict(self._counters),
                "histograms": {
                    name: {
                        "count": len(values),
                        "sum": sum(values),
                        "min": min(values) if values else 0.0,
                        "max": max(values) if values else 0.0,
                        "avg": sum(values) / len(values) if values else 0.0,
                    }
                    for name, values in self._histograms.items()
                },
            }

    def get_metrics_text(self) -> str:
        """Format all metrics as Prometheus-style text.

        Returns
        -------
        str
            Text suitable for a ``/metrics`` HTTP response.
        """
        lines: list[str] = []
        now = _time.time()
        uptime = now - self._start_time

        with self._lock:
            # Uptime (always present)
            lines.append("# HELP quad_uptime_seconds Bot uptime in seconds")
            lines.append("# TYPE quad_uptime_seconds gauge")
            lines.append(f"quad_uptime_seconds {uptime:.2f}")

            # Gauges
            for name, value in self._gauges.items():
                labels = self._gauge_labels.get(name, {})
                label_str = _format_labels(labels)

                metric_name = name.replace("-", "_").replace(" ", "_").lower()
                lines.append(f"# HELP {metric_name} Gauge metric")
                lines.append(f"# TYPE {metric_name} gauge")
                lines.append(f"{metric_name}{label_str} {value}")

            # Counters
            for name, value in self._counters.items():
                labels = self._counter_labels.get(name, {})
                label_str = _format_labels(labels)

                metric_name = name.replace("-", "_").replace(" ", "_").lower()
                lines.append(f"# HELP {metric_name} Counter metric")
                lines.append(f"# TYPE {metric_name} counter")
                lines.append(f"{metric_name}{label_str} {value}")

            # Histograms (summary stats)
            for name, values in self._histograms.items():
                labels = self._histogram_labels.get(name, {})
                label_str = _format_labels(labels)

                metric_name = name.replace("-", "_").replace(" ", "_").lower()
                lines.append(f"# HELP {metric_name} Histogram metric")
                lines.append(f"# TYPE {metric_name} histogram")

                if values:
                    count = len(values)
                    total = sum(values)
                    sorted_vals = sorted(values)
                    # Prometheus histogram spec requires _bucket, _count, _sum
                    # Define bucket boundaries for typical trading metrics
                    buckets = [0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0, float("inf")]
                    for b in buckets:
                        le = "+Inf" if b == float("inf") else str(b)
                        bucket_count = sum(1 for v in sorted_vals if v <= b)
                        lines.append(f'{metric_name}_bucket{label_str}{{le="{le}"}} {bucket_count}')
                    lines.append(f'{metric_name}_count{label_str} {count}')
                    lines.append(f'{metric_name}_sum{label_str} {total}')
                    if count > 0:
                        lines.append(f'{metric_name}_min{label_str} {sorted_vals[0]}')
                        lines.append(f'{metric_name}_max{label_str} {sorted_vals[-1]}')
                        lines.append(f'{metric_name}_avg{label_str} {total / count:.4f}')

        lines.append("")
        return "\n".join(lines)

    def reset(self) -> None:
        """Reset all metrics to their initial state (for testing)."""
        with self._lock:
            self._gauges.clear()
            self._counters.clear()
            self._histograms.clear()
            self._gauge_labels.clear()
            self._counter_labels.clear()
            self._histogram_labels.clear()
            self._start_time = _time.time()

    @property
    def uptime(self) -> float:
        """Return uptime in seconds."""
        return _time.time() - self._start_time


# ============================================================================
# Helpers
# ============================================================================


def _format_labels(labels: dict[str, str]) -> str:
    """Format a label dict as a Prometheus label string.

    Returns ``""`` for empty labels, or ``'{key="val",key2="val2"}'``.
    """
    if not labels:
        return ""
    parts = [f'{k}="{v}"' for k, v in sorted(labels.items())]
    return "{" + ",".join(parts) + "}"
