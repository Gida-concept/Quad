"""Prediction-quality metrics for AI trading decisions.

Phase 3 of the AI-decision quality upgrade.  This module is the measurement
loop the original request demanded: *"how do we make sure the AI predicts
price movements"*.  It computes, from the ``decisions`` table:

- **Hit rate** (directional accuracy) — fraction of resolved ENTER decisions
  whose ``predicted_direction`` matched the actual price move.
- **Expected Calibration Error (ECE)** — mean |mean-confidence − accuracy| per
  confidence bin, weighted by bin size.  Standard formulation (10 bins of 0.1
  by default).
- **Brier score** — mean squared error between the predicted probability
  (confidence) and the actual binary outcome.

All functions are PURE (no I/O, no network, no database) and therefore
offline-testable, mirroring ``quad.ai.validator``'s style: module-level pure
functions plus a thin caller (``compute_metrics``).  There is NO dependency on
the exchange adapter — this is measurement over the decision rows, not live
trading.

Win / loss / flat semantics (documented decision)
-------------------------------------------------
Input rows carry an ``outcome`` column with one of four labels:

- ``"open"``  — the decision is still unresolved (its position has not
  closed).  EXCLUDED from every metric.
- ``"win"``   — the direction forecast was correct (position closed
  profitably in the predicted direction).  Counts as a hit.
- ``"loss"``  — the direction forecast was incorrect.  Counts as a miss.
- ``"flat"``  — EXCLUDED from the hit-rate / ECE / Brier denominators.

Why ``"flat"`` is excluded rather than counted as a miss: a ``flat`` row is
assigned by ``QuadOrchestrator._reconcile_decision_outcomes`` whenever the
decision's symbol disappears from the live position set.  Today that conflates
"closed at breakeven" with "symbol rotation moved on while the decision was
still open" — and because realized PnL is not backfilled (see below) the
overwhelming majority of resolved rows are ``flat``.  Counting ``flat`` as a
miss would therefore drive the hit rate toward zero (and inflate ECE) for
reasons entirely unrelated to forecast skill.  Excluding it keeps the headline
metrics a clean measure of directional forecasting ability over decisions that
were actually given a directional resolution.

Data-quality limitation (be honest about this)
----------------------------------------------
``realized_pnl`` / ``exit_price`` are NOT backfilled: positions close
on-exchange via the TP/SL bracket orders attached at entry, there is no local
fill/close path, and the exchange adapter exposes no income-history API.  The
``realizedProfit`` value surfaced by ``get_positions()`` only exists for
still-open positions and is the cumulative symbol/account figure, not the
per-trade PnL of a single decision.  Consequently the win/loss labels in the
database are only as good as whatever assigned them; today the reconciliation
pass marks every disappearance ``flat``, so hit rate / ECE / Brier will return
``None`` until win/loss assignment is implemented.  The module is correct and
useful TODAY for measuring whatever win/loss labels exist, and the ``entry_price``
capture added in Phase 3 improves future backfillability.

Rows may be ``DecisionModel`` instances or plain dicts.  The only attributes
read are ``predicted_direction``, ``confidence``, and ``outcome``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

#: Outcomes that count as a resolved directional bet (used in every metric).
_DIRECTIONAL_OUTCOMES: frozenset[str] = frozenset({"win", "loss"})

#: Directions that carry a directional bet.  NEUTRAL is excluded: a NEUTRAL
#: ENTER is un-derivable (rejected by ``normalize_decision``) so it can never
#: resolve to a win/loss, and including it would corrupt direction accuracy.
_DIRECTIONAL_DIRECTIONS: frozenset[str] = frozenset({"LONG", "SHORT"})


@dataclass
class PredictionMetrics:
    """Metrics over a set of AI decision rows.

    All ratio metrics are ``None`` when their denominator is empty (e.g. no
    resolved directional rows yet).
    """

    sample_count: int = 0
    """Total number of rows fed in (including unresolved)."""

    resolved_count: int = 0
    """Rows with ``outcome != 'open'``."""

    directional_count: int = 0
    """Rows that are a resolved directional bet (win/loss AND LONG/SHORT)."""

    wins: int = 0
    """Number of correct direction forecasts (outcome == 'win')."""

    losses: int = 0
    """Number of incorrect direction forecasts (outcome == 'loss')."""

    flat_count: int = 0
    """Rows with ``outcome == 'flat'`` (excluded from directional metrics)."""

    open_count: int = 0
    """Rows still ``outcome == 'open'`` (excluded from every metric)."""

    hit_rate: float | None = None
    """``wins / (wins + losses)`` — directional accuracy, ``None`` when empty."""

    ece: float | None = None
    """Expected Calibration Error over confidence bins, ``None`` when empty."""

    ece_bins: list[dict[str, Any]] = field(default_factory=list)
    """Per-bin diagnostics: bin index, lo/hi edges, count, avg_confidence,
    avg_accuracy."""

    brier: float | None = None
    """Brier score = mean((confidence − outcome)²), ``None`` when empty."""

    mean_confidence: float | None = None
    """Mean predicted confidence over the directional rows."""


# ---------------------------------------------------------------------------
# Internal helpers (duck-typed row access)
# ---------------------------------------------------------------------------


def _row_attr(row: Any, name: str, default: Any = None) -> Any:
    """Read an attribute from a decision row (object or dict)."""
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def _outcome(row: Any) -> str:
    """Normalized lowercase outcome label."""
    return str(_row_attr(row, "outcome", "open") or "open").strip().lower()


def _direction(row: Any) -> str:
    """Normalized uppercase predicted direction."""
    return str(
        _row_attr(row, "predicted_direction", "NEUTRAL") or "NEUTRAL"
    ).strip().upper()


def _confidence(row: Any) -> float:
    """Coerce + clamp a row's confidence to ``[0, 1]`` (default ``0.0``)."""
    try:
        conf = float(_row_attr(row, "confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    return max(0.0, min(1.0, conf))


def _is_resolved(row: Any) -> bool:
    """True when the row has a non-open outcome."""
    return _outcome(row) != "open"


def _is_directional(row: Any) -> bool:
    """True when the row is a resolved directional bet usable in metrics.

    Requires ``outcome`` in {win, loss} AND a LONG/SHORT prediction.  Rows
    predicted NEUTRAL (or with an unknown direction) are not a directional
    bet and are excluded from hit rate / ECE / Brier.
    """
    return (
        _outcome(row) in _DIRECTIONAL_OUTCOMES
        and _direction(row) in _DIRECTIONAL_DIRECTIONS
    )


# ---------------------------------------------------------------------------
# Individual metrics
# ---------------------------------------------------------------------------


def hit_rate(rows: Sequence[Any]) -> float | None:
    """Directional accuracy: ``wins / (wins + losses)``.

    Returns ``None`` when there are no resolved directional rows.  ``flat``
    and ``open`` rows are excluded from the denominator.
    """
    wins = 0
    losses = 0
    for row in rows:
        if not _is_directional(row):
            continue
        if _outcome(row) == "win":
            wins += 1
        else:
            losses += 1
    denominator = wins + losses
    if denominator == 0:
        return None
    return wins / denominator


def expected_calibration_error(
    rows: Sequence[Any],
    n_bins: int = 10,
) -> tuple[float | None, list[dict[str, Any]]]:
    """Expected Calibration Error over confidence bins.

    Buckets the *directional* rows by predicted confidence into ``n_bins``
    bins of width ``1 / n_bins`` and computes the weighted mean of
    ``|avg_accuracy - avg_confidence|`` per bin (weight = bin size / N).

    Parameters
    ----------
    rows:
        Decision rows (only resolved directional rows are used).
    n_bins:
        Number of equal-width confidence bins (default 10).

    Returns
    -------
    tuple[float | None, list[dict[str, Any]]]
        ``(ece, bins)``.  ``ece`` is ``None`` when no directional rows exist.
        ``bins`` is a list of per-bin diagnostics dicts.
    """
    if n_bins <= 0:
        n_bins = 10

    directional = [r for r in rows if _is_directional(r)]
    n = len(directional)
    if n == 0:
        return None, []

    confidences = [_confidence(r) for r in directional]
    outcomes = [_outcome(r) for r in directional]

    total_error = 0.0
    bins: list[dict[str, Any]] = []
    for i in range(n_bins):
        lo = i / n_bins
        hi = (i + 1) / n_bins
        indices = [
            j
            for j, c in enumerate(confidences)
            if (lo <= c < hi) or (i == n_bins - 1 and c == 1.0)
        ]
        if not indices:
            continue
        bin_conf = sum(confidences[j] for j in indices) / len(indices)
        bin_acc = sum(1.0 if outcomes[j] == "win" else 0.0 for j in indices) / len(
            indices
        )
        weight = len(indices) / n
        total_error += weight * abs(bin_acc - bin_conf)
        bins.append(
            {
                "bin": i,
                "lo": lo,
                "hi": hi,
                "count": len(indices),
                "avg_confidence": bin_conf,
                "avg_accuracy": bin_acc,
            }
        )

    return total_error, bins


def brier_score(rows: Sequence[Any]) -> float | None:
    """Brier score over the resolved directional rows.

    ``mean((confidence - outcome) ** 2)`` with outcome encoded as ``1.0`` for
    a win and ``0.0`` for a loss.  A perfectly confident coin-flip model
    (always 0.5) scores ~0.25.  Returns ``None`` with no directional rows.
    """
    total = 0.0
    count = 0
    for row in rows:
        if not _is_directional(row):
            continue
        outcome = 1.0 if _outcome(row) == "win" else 0.0
        total += (_confidence(row) - outcome) ** 2
        count += 1
    if count == 0:
        return None
    return total / count


# ---------------------------------------------------------------------------
# Aggregate entry point
# ---------------------------------------------------------------------------


def compute_metrics(
    rows: Sequence[Any],
    n_bins: int = 10,
) -> PredictionMetrics:
    """Compute all prediction-quality metrics for a set of decision rows.

    Parameters
    ----------
    rows:
        Sequence of decision records (``DecisionModel`` instances or dicts).
        Only ``predicted_direction``, ``confidence``, and ``outcome`` are read.
    n_bins:
        Number of ECE confidence bins (default 10).

    Returns
    -------
    PredictionMetrics
        Populated dataclass.  Ratio metrics are ``None`` when their
        denominators are empty.  Never raises on empty / malformed input.
    """
    rows = list(rows or [])

    metrics = PredictionMetrics(sample_count=len(rows))
    metrics.open_count = sum(1 for r in rows if not _is_resolved(r))
    metrics.flat_count = sum(1 for r in rows if _outcome(r) == "flat")
    metrics.resolved_count = len(rows) - metrics.open_count
    # Wins/losses/directional_count count only DIRECTIONAL rows (LONG/SHORT
    # with a resolved outcome).  A NEUTRAL prediction is not a directional bet
    # (an un-derivable ENTER): it must never inflate these counts, which gate
    # the orchestrator's ``min_resolved`` and would otherwise disagree with
    # hit_rate/ECE/Brier (all of which exclude NEUTRAL via ``_is_directional``).
    metrics.wins = sum(
        1 for r in rows if _is_directional(r) and _outcome(r) == "win"
    )
    metrics.losses = sum(
        1 for r in rows if _is_directional(r) and _outcome(r) == "loss"
    )
    metrics.directional_count = metrics.wins + metrics.losses

    metrics.hit_rate = hit_rate(rows)
    metrics.ece, metrics.ece_bins = expected_calibration_error(rows, n_bins)
    metrics.brier = brier_score(rows)

    confidences = [_confidence(r) for r in rows if _is_directional(r)]
    if confidences:
        metrics.mean_confidence = sum(confidences) / len(confidences)

    return metrics
