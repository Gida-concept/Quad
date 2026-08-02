"""Deterministic direction-to-side validation for AI trading decisions.

Phase 1 of the inversion-proof upgrade.  The LLM forecasts a *direction*
(LONG / SHORT / NEUTRAL); this module deterministically derives the order
*side* (BUY / SELL) from that direction so that a long/short buy/sell
inversion is impossible by construction.

All functions in this module are pure (no I/O, no network, no database) and
therefore offline-testable.  The only runtime dependency is on the caller
supplying the current position side and, optionally, technical indicators
for the plausibility gate.

Public API
----------
- :func:`canonical_direction` — map raw model text to LONG / SHORT / NEUTRAL.
- :func:`derive_side` — deterministic action+direction+position -> side.
- :func:`plausibility_check` — veto LONG/SHORT entries that fight the trend.
- :func:`normalize_decision` — full decision normalization returning a
  :class:`ValidationResult`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

logger = structlog.get_logger(__name__)

# Canonical direction label.
Direction = Literal["LONG", "SHORT", "NEUTRAL"]

# Order side labels emitted by :func:`derive_side`.
_SIDE_BUY = "BUY"
_SIDE_SELL = "SELL"

# Actions the AI may emit.  ENTER/EXIT carry a side; the rest are no-ops or
# position-management actions that do not open/close a position.
VALID_ACTIONS: frozenset[str] = frozenset(
    {"ENTER", "EXIT", "HOLD", "adjust_stop", "reduce_position"}
)

# Words that map to a canonical direction.  All comparisons are on the
# lower-cased, stripped input token.
_LONG_TOKENS: frozenset[str] = frozenset(
    {"long", "buy", "up", "bullish", "bull", "buying", "b"}
)
_SHORT_TOKENS: frozenset[str] = frozenset(
    {"short", "sell", "down", "bearish", "bear", "selling", "s"}
)
_NEUTRAL_TOKENS: frozenset[str] = frozenset(
    {"neutral", "flat", "hold", "none", "sideways", "range", "ranging", ""}
)

# Indicator keys consumed by the plausibility gate.
_TREND_REGIME_KEY = "trend_regime"
_RSI_KEY = "momentum_rsi_14"

# Trend-regime values that count as a downtrend / uptrend for the gate.
_DOWNTREND_REGIMES: frozenset[str] = frozenset({"downtrend", "weak_downtrend"})
_UPTREND_REGIMES: frozenset[str] = frozenset({"uptrend", "weak_uptrend"})


@dataclass
class ValidationResult:
    """Outcome of :func:`normalize_decision`.

    Attributes
    ----------
    decision:
        The normalized decision dict.  When ``ok`` is ``False`` the caller
        must discard this and substitute a safe HOLD.
    ok:
        ``True`` when the decision may proceed; ``False`` when it must be
        rejected.
    rejected_reason:
        Human-readable reason populated when ``ok`` is ``False``.
    corrected:
        List of field names the validator corrected (e.g. ``["side"]`` when
        an AI-supplied side conflicted with the derived side).
    """

    decision: dict[str, Any]
    ok: bool
    rejected_reason: str = ""
    corrected: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _normalize_position_side(position_side: Any) -> str | None:
    """Return ``"LONG"``/``"SHORT"`` for an open position, else ``None``.

    Accepts ``None``, ``"LONG"``/``"SHORT"`` (any case), ``PositionSide``
    enums, and flat/closed/neutral tokens which all map to ``None``.

    Parameters
    ----------
    position_side:
        The side of the currently held position, or ``None`` when flat.

    Returns
    -------
    str or None
        ``"LONG"`` or ``"SHORT"``, or ``None`` when flat / unknown.
    """
    if position_side is None:
        return None
    # Enum-like objects (e.g. quad.types.domain.PositionSide).
    if not isinstance(position_side, str):
        name = getattr(position_side, "name", None) or getattr(
            position_side, "value", None
        )
        return _normalize_position_side(name)

    token = position_side.strip().upper()
    if token in ("LONG", "SHORT"):
        return token
    # Anything else (flat / none / closed / neutral / unknown) is treated as
    # "no open position", which is the safe assumption for side derivation.
    return None


# ---------------------------------------------------------------------------
# Public pure functions
# ---------------------------------------------------------------------------


def canonical_direction(raw: Any) -> Direction:
    """Map raw model output to a canonical direction.

    Recognises common synonyms for each direction and defaults unknown /
    missing input to ``NEUTRAL`` (the safe, no-op direction).

    Parameters
    ----------
    raw:
        The raw value emitted by the LLM, e.g. ``"long"``, ``"BUY"``,
        ``"bearish"``, ``"flat"``, ``"hold"``, or ``None``.

    Returns
    -------
    Direction
        ``"LONG"``, ``"SHORT"``, or ``"NEUTRAL"``.
    """
    if raw is None:
        return "NEUTRAL"
    # Enum-like objects (e.g. a Direction enum or PositionSide).
    if not isinstance(raw, str):
        name = getattr(raw, "name", None) or getattr(raw, "value", None)
        return canonical_direction(name)

    token = raw.strip().lower()
    if token in _LONG_TOKENS:
        return "LONG"
    if token in _SHORT_TOKENS:
        return "SHORT"
    if token in _NEUTRAL_TOKENS:
        return "NEUTRAL"

    # Lenient substring fallback for compound phrases the model may emit
    # (e.g. "long-biased", "short-term pullback").  Match on the leading
    # keyword only so we never guess from an arbitrary substring.
    leading = token.split()[0] if token.split() else token
    if leading in ("long", "buy", "bull", "up"):
        return "LONG"
    if leading in ("short", "sell", "bear", "down"):
        return "SHORT"

    logger.debug(
        "ai_direction_unknown_defaulting_neutral",
        raw=str(raw)[:40],
    )
    return "NEUTRAL"


def derive_side(
    action: str,
    direction: Any,
    position_side: Any = None,
) -> str | None:
    """Derive the deterministic order side from action + direction + position.

    This is the core inversion guard: the order side is *never* taken from
    the LLM verbatim — it is computed from the model's directional forecast
    and the currently held position.

    Rules
    -----
    ENTER:
        LONG -> ``"BUY"``, SHORT -> ``"SELL"``, NEUTRAL -> ``None`` (un-derivable).
    EXIT:
        closes whatever is held — held LONG -> ``"SELL"``, held SHORT ->
        ``"BUY"``; NEUTRAL direction closes the held position; flat ->
        ``None`` (nothing to close).
    HOLD / adjust_stop / reduce_position:
        ``None`` (no deterministic side; these do not open/close).

    Parameters
    ----------
    action:
        The decision action, e.g. ``"ENTER"``.
    direction:
        Raw direction value (canonicalized internally).
    position_side:
        Side of the currently held position, or ``None`` when flat.

    Returns
    -------
    str or None
        ``"BUY"``, ``"SELL"``, or ``None`` when the side cannot be derived.
    """
    action_key = (action or "").strip().upper()
    direction_key = canonical_direction(direction)

    if action_key in ("HOLD", "ADJUST_STOP", "REDUCE_POSITION"):
        return None

    if action_key == "ENTER":
        if direction_key == "LONG":
            return _SIDE_BUY
        if direction_key == "SHORT":
            return _SIDE_SELL
        return None  # NEUTRAL entry has no derivable side.

    if action_key == "EXIT":
        held = _normalize_position_side(position_side)
        if held is None:
            return None  # flat — nothing to close.
        return _SIDE_SELL if held == "LONG" else _SIDE_BUY

    return None  # Unknown action: no side.


def plausibility_check(
    direction: Any,
    indicators: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Sanity-check a directional entry against the technical backdrop.

    Rejects entries that fight the prevailing trend at an extreme RSI:

    - LONG into a downtrend with RSI > 70 (overbought) is vetoed.
    - SHORT into an uptrend with RSI < 30 (oversold) is vetoed.

    The check is tolerant of missing values: if ``indicators`` is ``None``,
    empty, or lacks ``trend_regime`` / ``momentum_rsi_14``, it passes.

    Parameters
    ----------
    direction:
        Raw direction value (canonicalized internally).
    indicators:
        Indicator dict produced by ``quad.ai.ta.compute_indicators``, or
        ``None``.

    Returns
    -------
    tuple[bool, str]
        ``(ok, reason)`` — ``ok`` is ``False`` when the entry is vetoed and
        ``reason`` explains why; otherwise ``(True, "")``.
    """
    direction_key = canonical_direction(direction)
    ind = indicators or {}

    trend = str(ind.get(_TREND_REGIME_KEY, "") or "").strip().lower()
    rsi_raw = ind.get(_RSI_KEY)
    try:
        rsi = float(rsi_raw) if rsi_raw is not None else None
    except (TypeError, ValueError):
        rsi = None

    if direction_key == "LONG" and trend in _DOWNTREND_REGIMES:
        if rsi is not None and rsi > 70:
            reason = (
                f"LONG into downtrend with overbought RSI "
                f"(trend_regime={trend}, rsi={rsi:.1f})"
            )
            return False, reason

    if direction_key == "SHORT" and trend in _UPTREND_REGIMES:
        if rsi is not None and rsi < 30:
            reason = (
                f"SHORT into uptrend with oversold RSI "
                f"(trend_regime={trend}, rsi={rsi:.1f})"
            )
            return False, reason

    return True, ""


def normalize_decision(
    decision: dict[str, Any],
    *,
    position_side: Any = None,
    indicators: dict[str, Any] | None = None,
    gate_mode: str = "warn",
    min_confidence_to_trade: float = 0.0,
) -> ValidationResult:
    """Validate and normalize an AI trading decision in place of a copy.

    Guarantees:
    - The decision's ``direction`` is canonicalized to LONG / SHORT / NEUTRAL
      (falling back to the legacy raw ``side`` for backward compatibility).
    - For ENTER / EXIT the ``side`` is *derived* deterministically from
      ``direction`` + ``position_side``.  An AI-supplied side that conflicts
      with the derived side is overridden and recorded in ``corrected``.
    - ENTER / EXIT with an un-derivable side (NEUTRAL entry, or EXIT with no
      open position) are rejected (``ok=False``).
    - HOLD passes through as a safe no-op with ``side=None`` and
      ``direction=NEUTRAL``.
    - ``confidence`` is clamped to ``[0, 1]``, defaulting to ``0.0``.
    - ``gate_result`` records the plausibility-gate outcome: ``"pass"``,
      ``"warn"`` (gate_mode ``"warn"``), ``"veto"`` (gate_mode ``"veto"``),
      or ``"not_checked"`` when no indicators were supplied.  When the
      min-confidence gate rejects, ``gate_result`` is ``"confidence"``.
    - In ``veto`` mode a plausibility veto rejects the decision.

    Min-confidence gate (Phase 2):
    - ENTER / EXIT decisions whose (clamped) confidence is below
      ``min_confidence_to_trade`` are REJECTED (``ok=False``) in BOTH gate
      modes.  Unlike the plausibility gate — which is advisory in ``warn``
      mode — the confidence gate is a hard rule: a low-conviction ENTER/EXIT
      must never reach execution.  ``gate_mode`` only changes how the
      rejection is logged (warning in ``warn`` mode, error in ``veto`` mode).
      The orchestrator's existing reject-to-safe-HOLD path downgrades the
      decision to HOLD for free.

    Parameters
    ----------
    decision:
        The parsed AI decision dict.
    position_side:
        Side of the currently held position, or ``None`` when flat.
    indicators:
        Optional indicator dict for the plausibility gate.
    gate_mode:
        ``"warn"`` (default) logs a veto condition without rejecting;
        ``"veto"`` rejects the decision.
    min_confidence_to_trade:
        Minimum confidence (0-1) for ENTER/EXIT.  ``0.0`` (default) disables
        the gate.  Uses the clamped confidence value.

    Returns
    -------
    ValidationResult
        The normalized decision plus outcome metadata.
    """
    out = dict(decision) if isinstance(decision, dict) else {}

    # --- Action -----------------------------------------------------------
    action = str(out.get("action") or "HOLD").strip()
    if action not in VALID_ACTIONS:
        logger.warning("ai_decision_unknown_action", action=action)
        action = "HOLD"
        out["action"] = "HOLD"

    corrected: list[str] = []
    rejected_reason = ""

    # --- Direction (canonicalize; tolerate legacy raw ``side`` fallback) ---
    raw_direction = out.get("direction")
    if raw_direction in (None, "") and out.get("side") not in (None, ""):
        raw_direction = out.get("side")
    direction = canonical_direction(raw_direction)

    # --- Side derivation --------------------------------------------------
    raw_side = out.get("side")
    derived_side = derive_side(action, direction, position_side)

    if action == "HOLD":
        # HOLD is a no-op: never expose a side or a directional bet.
        out["side"] = None
        out["direction"] = "NEUTRAL"
    elif action in ("adjust_stop", "reduce_position"):
        # Position-management actions: no deterministic derivation.  Preserve
        # whatever side the model supplied for the management order.
        out["direction"] = direction
    else:  # ENTER / EXIT
        out["direction"] = direction
        if derived_side is None:
            rejected_reason = _un_derivable_reason(action, direction, position_side)
        else:
            if raw_side not in (None, "") and _side_token(raw_side) != derived_side:
                corrected.append("side")
                logger.warning(
                    "ai_side_corrected",
                    ai_side=raw_side,
                    derived_side=derived_side,
                    action=action,
                    direction=direction,
                )
            out["side"] = derived_side

    # --- Confidence (clamp to [0, 1]; default 0.0) -------------------------
    try:
        confidence = float(out.get("confidence")) if out.get("confidence") is not None else 0.0
    except (TypeError, ValueError):
        confidence = 0.0
    out["confidence"] = max(0.0, min(1.0, confidence))

    # --- Plausibility gate -------------------------------------------------
    if indicators:
        gate_ok, gate_reason = plausibility_check(direction, indicators)
        if not gate_ok:
            if str(gate_mode).strip().lower() == "veto":
                out["gate_result"] = "veto"
                if not rejected_reason:
                    rejected_reason = gate_reason
            else:
                out["gate_result"] = "warn"
                logger.warning("ai_plausibility_warn", reason=gate_reason)
        else:
            out["gate_result"] = "pass"
    else:
        out["gate_result"] = "not_checked"

    # --- Min-confidence gate (Phase 2) -------------------------------------
    # Hard rule: ENTER / EXIT below the threshold never reach execution.
    # Uses the CLAMPED confidence computed above.  Unlike the plausibility
    # gate (which is advisory in warn mode), the confidence gate rejects in
    # BOTH gate modes; gate_mode only changes the log level.
    try:
        min_conf = float(min_confidence_to_trade)
    except (TypeError, ValueError):
        min_conf = 0.0
    min_conf = max(0.0, min(1.0, min_conf))

    if min_conf > 0.0 and action in ("ENTER", "EXIT") and confidence < min_conf:
        conf_reason = (
            f"Confidence {confidence:.2f} is below "
            f"min_confidence_to_trade {min_conf:.2f} for {action}."
        )
        if not rejected_reason:
            out["gate_result"] = "confidence"
            rejected_reason = conf_reason
        if str(gate_mode).strip().lower() == "veto":
            logger.error(
                "ai_confidence_veto",
                reason=conf_reason,
                confidence=confidence,
                min_confidence=min_conf,
            )
        else:
            logger.warning(
                "ai_confidence_warn",
                reason=conf_reason,
                confidence=confidence,
                min_confidence=min_conf,
            )

    return ValidationResult(
        decision=out,
        ok=not bool(rejected_reason),
        rejected_reason=rejected_reason,
        corrected=corrected,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _side_token(side: Any) -> str:
    """Normalize a raw side value to ``"BUY"`` or ``"SELL"``.

    Unknown values fall back to ``""`` so comparisons fail and the derived
    side wins (recorded in ``corrected``).
    """
    if isinstance(side, str):
        token = side.strip().upper()
        if token in ("BUY", "SELL", "LONG", "SHORT"):
            return "BUY" if token in ("BUY", "LONG") else "SELL"
    return ""


def _un_derivable_reason(action: str, direction: Direction, position_side: Any) -> str:
    """Build a human-readable rejection reason for an un-derivable side."""
    if action == "ENTER":
        return (
            f"ENTER requires a directional forecast; got direction={direction}. "
            "Order side cannot be derived deterministically."
        )
    if action == "EXIT":
        held = _normalize_position_side(position_side)
        if held is None:
            return (
                "EXIT requested but no open position is held for the contract. "
                "Order side cannot be derived deterministically."
            )
        return (
            f"EXIT side un-derivable for direction={direction} "
            f"position={held}."
        )
    return f"Order side un-derivable for action={action} direction={direction}."
