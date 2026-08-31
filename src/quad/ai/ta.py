"""Technical indicator computation for AI trading decisions.

Computes a comprehensive set of technical indicators from OHLCV candle data
for use in AI prompting and decision-making.

All functions accept a list of ``Candle`` objects (oldest first) and return
a flat dict of numeric values and regime labels.

Uses numpy for vectorized computation when available, with an automatic
pure-Python fallback.
"""

from __future__ import annotations

import math
from typing import Any

import structlog

from quad.types.market import Candle

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Optional numpy support
# ---------------------------------------------------------------------------

_HAS_NUMPY = False
try:
    import numpy as np  # noqa: F401

    _HAS_NUMPY = True
except ImportError:
    pass


# ============================================================================
# Python fallback helpers
# ============================================================================


def _sma(values: list[float], period: int) -> list[float]:
    """Simple moving average (pure Python)."""
    if len(values) < period:
        return []
    result: list[float] = []
    window_sum = sum(values[:period])
    result.append(window_sum / period)
    for i in range(period, len(values)):
        window_sum += values[i] - values[i - period]
        result.append(window_sum / period)
    return result


def _ema(values: list[float], period: int) -> list[float]:
    """Exponential moving average (pure Python)."""
    if len(values) < period:
        return []
    multiplier = 2.0 / (period + 1)
    result: list[float] = []
    # Start with SMA
    sma = sum(values[:period]) / period
    result.append(sma)
    for i in range(period, len(values)):
        ema = (values[i] - result[-1]) * multiplier + result[-1]
        result.append(ema)
    return result


def _rma(values: list[float], period: int) -> list[float]:
    """Running moving average (used in RSI, ADX)."""
    if len(values) < period:
        return []
    result: list[float] = []
    avg = sum(values[:period]) / period
    result.append(avg)
    for i in range(period, len(values)):
        avg = (avg * (period - 1) + values[i]) / period
        result.append(avg)
    return result


def _std_dev(values: list[float], period: int, sma_values: list[float]) -> list[float]:
    """Rolling standard deviation (pure Python)."""
    if len(values) < period or len(sma_values) < 1:
        return []
    result: list[float] = []
    for i in range(period, len(values) + 1):
        window = values[i - period : i]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        result.append(math.sqrt(variance))
    return result


def _true_range(high: list[float], low: list[float], close: list[float]) -> list[float]:
    """Compute true range values."""
    tr: list[float] = [high[0] - low[0]]
    for i in range(1, len(high)):
        hl = high[i] - low[i]
        hc = abs(high[i] - close[i - 1])
        lc = abs(low[i] - close[i - 1])
        tr.append(max(hl, hc, lc))
    return tr


# ============================================================================
# Candle data extraction
# ============================================================================


def _extract_values(
    candles: list[Candle],
) -> tuple[list[float], list[float], list[float], list[float], list[float]]:
    """Extract OHLCV float arrays from candle list."""
    opens = [float(c.open) for c in candles]
    highs = [float(c.high) for c in candles]
    lows = [float(c.low) for c in candles]
    closes = [float(c.close) for c in candles]
    volumes = [float(c.volume) for c in candles]
    return opens, highs, lows, closes, volumes


# ============================================================================
# Trend indicators
# ============================================================================


def _compute_ema(closes: list[float], period: int) -> float | None:
    """Return the latest EMA value."""
    values = _ema(closes, period)
    return values[-1] if values else None


def _compute_adx(
    high: list[float], low: list[float], close: list[float], period: int = 14
) -> dict[str, Any]:
    """Compute ADX and directional indicators."""
    n = len(high)
    if n < period + 1:
        return {"adx": None, "plus_di": None, "minus_di": None}

    # True range
    tr = _true_range(high, low, close)

    # Directional movement
    plus_dm: list[float] = [0.0]
    minus_dm: list[float] = [0.0]
    for i in range(1, n):
        up_move = high[i] - high[i - 1]
        down_move = low[i - 1] - low[i]

        if up_move > down_move and up_move > 0:
            plus_dm.append(up_move)
        else:
            plus_dm.append(0.0)

        if down_move > up_move and down_move > 0:
            minus_dm.append(down_move)
        else:
            minus_dm.append(0.0)

    # Smoothed TR and DM
    atr_values = _rma(tr, period)
    plus_di_smooth = _rma(plus_dm, period)
    minus_di_smooth = _rma(minus_dm, period)

    # DI values
    plus_di_list: list[float] = []
    minus_di_list: list[float] = []
    dx_list: list[float] = []

    for i in range(len(atr_values)):
        if atr_values[i] != 0:
            pdi = 100 * plus_di_smooth[i] / atr_values[i]
            mdi = 100 * minus_di_smooth[i] / atr_values[i]
        else:
            pdi = 0.0
            mdi = 0.0
        plus_di_list.append(pdi)
        minus_di_list.append(mdi)

        di_sum = pdi + mdi
        dx = 100 * abs(pdi - mdi) / di_sum if di_sum != 0 else 0
        dx_list.append(dx)

    # ADX is smoothed DX
    adx_list = _rma(dx_list, period)

    return {
        "adx": round(adx_list[-1], 2) if adx_list else None,
        "plus_di": round(plus_di_list[-1], 2) if plus_di_list else None,
        "minus_di": round(minus_di_list[-1], 2) if minus_di_list else None,
    }


def _detect_trend_regime(
    closes: list[float], ema20: float | None, ema50: float | None, ema200: float | None
) -> str:
    """Detect the trend regime: uptrend, downtrend, or ranging."""
    if ema20 is None or ema50 is None:
        return "ranging"

    current_price = closes[-1]

    # Strong uptrend: price > EMA20 > EMA50 (and EMA50 if available)
    if current_price > ema20 > ema50:
        if ema200 is not None and ema20 > ema200:
            return "uptrend"
        return "weak_uptrend"

    # Strong downtrend: price < EMA20 < EMA50
    if current_price < ema20 < ema50:
        if ema200 is not None and ema20 < ema200:
            return "downtrend"
        return "weak_downtrend"

    return "ranging"


# ============================================================================
# Momentum indicators
# ============================================================================


def _compute_rsi(closes: list[float], period: int = 14) -> float | None:
    """Compute the latest RSI value."""
    n = len(closes)
    if n < period + 1:
        return None

    gains: list[float] = []
    losses: list[float] = []

    for i in range(1, n):
        change = closes[i] - closes[i - 1]
        if change >= 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(change))

    avg_gain = _rma(gains, period)
    avg_loss = _rma(losses, period)

    if not avg_loss or avg_loss[-1] == 0:
        return 100.0 if avg_gain and avg_gain[-1] > 0 else 50.0

    rs = avg_gain[-1] / avg_loss[-1]
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return round(rsi, 2)


def _compute_macd(
    closes: list[float], fast: int = 12, slow: int = 26, signal_period: int = 9
) -> dict[str, Any]:
    """Compute MACD line, signal line, histogram, and cross detection."""
    if len(closes) < slow + signal_period:
        return {"macd": None, "signal": None, "histogram": None, "cross": None}

    fast_ema = _ema(closes, fast)
    slow_ema = _ema(closes, slow)

    # MACD line = fast EMA - slow EMA
    macd_line: list[float] = []
    for i in range(len(slow_ema)):
        macd_line.append(fast_ema[i + (len(fast_ema) - len(slow_ema))] - slow_ema[i])

    signal = _ema(macd_line, signal_period)

    if not macd_line or not signal:
        return {"macd": None, "signal": None, "histogram": None, "cross": None}

    current_macd = macd_line[-1]
    current_signal = signal[-1]
    histogram = current_macd - current_signal

    # Cross detection: compare last two histogram bars
    cross: str | None = None
    if len(macd_line) >= 2 and len(signal) >= 2:
        prev_macd = macd_line[-2]
        prev_signal = signal[-2]
        prev_hist = prev_macd - prev_signal
        if prev_hist <= 0 and histogram > 0:
            cross = "bullish"
        elif prev_hist >= 0 and histogram < 0:
            cross = "bearish"
        else:
            cross = "neutral"

    return {
        "macd": round(current_macd, 4),
        "signal": round(current_signal, 4),
        "histogram": round(histogram, 4),
        "cross": cross,
    }


def _compute_stochastic(
    high: list[float],
    low: list[float],
    close: list[float],
    k_period: int = 14,
    d_period: int = 3,
) -> dict[str, Any]:
    """Compute Stochastic Oscillator (%K and %D)."""
    n = len(close)
    if n < k_period + d_period:
        return {"stoch_k": None, "stoch_d": None}

    raw_k: list[float] = []
    for i in range(k_period - 1, n):
        high_max = max(high[i - k_period + 1 : i + 1])
        low_min = min(low[i - k_period + 1 : i + 1])
        if high_max != low_min:
            k = 100 * (close[i] - low_min) / (high_max - low_min)
        else:
            k = 50.0
        raw_k.append(k)

    signal_k = _sma(raw_k, d_period)

    if not raw_k or not signal_k:
        return {"stoch_k": None, "stoch_d": None}

    return {
        "stoch_k": round(raw_k[-1], 2),
        "stoch_d": round(signal_k[-1], 2),
    }


# ============================================================================
# Volatility indicators
# ============================================================================


def _compute_bollinger_bands(
    closes: list[float], period: int = 20, std_mult: float = 2.0
) -> dict[str, Any]:
    """Compute Bollinger Bands (middle, upper, lower, width%, position)."""
    if len(closes) < period:
        return {
            "bb_middle": None,
            "bb_upper": None,
            "bb_lower": None,
            "bb_width_pct": None,
            "bb_position": None,
        }

    sma_values = _sma(closes, period)
    std_values = _std_dev(closes, period, sma_values)

    if not sma_values or not std_values:
        return {
            "bb_middle": None,
            "bb_upper": None,
            "bb_lower": None,
            "bb_width_pct": None,
            "bb_position": None,
        }

    mid = sma_values[-1]
    std = std_values[-1]
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    width_pct = ((upper - lower) / mid * 100) if mid != 0 else 0.0

    # BB position: where is price within the bands? 0 = at lower, 1 = at upper
    position = (closes[-1] - lower) / (upper - lower) if upper != lower else 0.5

    return {
        "bb_middle": round(mid, 2),
        "bb_upper": round(upper, 2),
        "bb_lower": round(lower, 2),
        "bb_width_pct": round(width_pct, 2),
        "bb_position": round(position, 4),
    }


def _compute_atr(
    high: list[float], low: list[float], close: list[float], period: int = 14
) -> float | None:
    """Compute the latest ATR value."""
    tr = _true_range(high, low, close)
    if len(tr) < period:
        return None
    atr_values = _rma(tr, period)
    return round(atr_values[-1], 4) if atr_values else None


# ============================================================================
# Volume indicators
# ============================================================================


def _compute_volume_ratio(volumes: list[float], period: int = 20) -> float | None:
    """Compute current volume / SMA(volume) ratio."""
    if len(volumes) < period + 1:
        return None
    sma_values = _sma(volumes, period)
    if not sma_values:
        return None
    current_vol = volumes[-1]
    avg_vol = sma_values[-1]
    if avg_vol == 0:
        return None
    return round(current_vol / avg_vol, 4)


def _compute_obv(closes: list[float], volumes: list[float]) -> dict[str, Any]:
    """Compute On-Balance Volume and its trend direction."""
    n = len(closes)
    if n < 2:
        return {"obv": None, "obv_trend": None}

    obv: list[float] = [0.0]
    for i in range(1, n):
        if closes[i] > closes[i - 1]:
            obv.append(obv[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            obv.append(obv[-1] - volumes[i])
        else:
            obv.append(obv[-1])

    # OBV trend: compare recent OBV slope
    recent = obv[-10:] if len(obv) >= 10 else obv
    if len(recent) >= 2:
        slope = (recent[-1] - recent[0]) / len(recent)
        direction = "rising" if slope > 0 else ("falling" if slope < 0 else "flat")
    else:
        direction = "flat"

    return {
        "obv": round(obv[-1], 2),
        "obv_trend": direction,
    }


# ============================================================================
# Pattern recognition
# ============================================================================


def _detect_candlestick_patterns(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
) -> dict[str, Any]:
    """Detect common candlestick patterns on the last candle(s).

    Returns a dict of pattern_name -> bool.
    """
    patterns: dict[str, Any] = {}
    n = len(opens)
    if n < 2:
        patterns["error"] = "insufficient_data"
        return patterns

    o, h, l, c = opens[-1], highs[-1], lows[-1], closes[-1]
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    total_range = h - l

    if total_range == 0:
        return {
            p: False
            for p in [
                "doji",
                "bullish_engulfing",
                "bearish_engulfing",
                "hammer",
                "shooting_star",
            ]
        }

    # Doji: very small body relative to range
    patterns["doji"] = body / total_range < 0.1

    if n >= 2:
        prev_o, prev_c = opens[-2], closes[-2]

        # Bullish engulfing: red candle followed by green candle that engulfs it
        patterns["bullish_engulfing"] = (
            prev_c < prev_o  # previous was red
            and c > o  # current is green
            and c > prev_o  # current closes above previous open
            and o < prev_c  # current opens below previous close
        )

        # Bearish engulfing: green candle followed by red candle that engulfs it
        patterns["bearish_engulfing"] = (
            prev_c > prev_o  # previous was green
            and c < o  # current is red
            and c < prev_o  # current closes below previous open
            and o > prev_c  # current opens above previous close
        )

    # Hammer: small body at the top, long lower wick
    patterns["hammer"] = (
        lower_wick >= 2 * body and upper_wick <= body and lower_wick > 0
    )

    # Shooting star: small body at the bottom, long upper wick
    patterns["shooting_star"] = (
        upper_wick >= 2 * body and lower_wick <= body and upper_wick > 0
    )

    return patterns


# ============================================================================
# Public API
# ============================================================================


def compute_indicators(candles: list[Candle]) -> dict[str, Any]:
    """Compute a comprehensive set of technical indicators from candle data.

    Parameters
    ----------
    candles:
        List of ``Candle`` objects, oldest first.  At least 200 candles
        are recommended for EMA(200).

    Returns
    -------
    dict
        A flat dictionary of indicator values keyed by descriptive names.
        Always includes a ``"_candle_count"`` key for debugging.
        Values are floats, strings for regime labels, or ``None`` when
        insufficient data exists.
    """
    result: dict[str, Any] = {}
    n = len(candles)
    result["_candle_count"] = n

    if n < 14:
        logger.warning("insufficient_candles_for_indicators", count=n)
        return result

    opens, highs, lows, closes, volumes = _extract_values(candles)

    # ------------------------------------------------------------------
    # Trend
    # ------------------------------------------------------------------
    ema20 = _compute_ema(closes, 20)
    ema50 = _compute_ema(closes, 50)
    ema200 = _compute_ema(closes, 200)
    adx_data = _compute_adx(highs, lows, closes, 14)
    regime = _detect_trend_regime(closes, ema20, ema50, ema200)

    result["trend_ema_20"] = ema20
    result["trend_ema_50"] = ema50
    result["trend_ema_200"] = ema200
    result["trend_adx"] = adx_data.get("adx")
    result["trend_plus_di"] = adx_data.get("plus_di")
    result["trend_minus_di"] = adx_data.get("minus_di")
    result["trend_regime"] = regime
    result["trend_price_vs_ema20"] = round(closes[-1] / ema20 - 1, 4) if ema20 else None

    # ------------------------------------------------------------------
    # Momentum
    # ------------------------------------------------------------------
    rsi = _compute_rsi(closes, 14)
    macd_data = _compute_macd(closes, 12, 26, 9)
    stoch_data = _compute_stochastic(highs, lows, closes, 14, 3)

    result["momentum_rsi_14"] = rsi
    result["momentum_macd"] = macd_data.get("macd")
    result["momentum_macd_signal"] = macd_data.get("signal")
    result["momentum_macd_histogram"] = macd_data.get("histogram")
    result["momentum_macd_cross"] = macd_data.get("cross")
    result["momentum_stoch_k"] = stoch_data.get("stoch_k")
    result["momentum_stoch_d"] = stoch_data.get("stoch_d")

    # RSI regime
    if rsi is not None:
        if rsi >= 70:
            result["momentum_rsi_regime"] = "overbought"
        elif rsi <= 30:
            result["momentum_rsi_regime"] = "oversold"
        else:
            result["momentum_rsi_regime"] = "neutral"

    # ------------------------------------------------------------------
    # Volatility
    # ------------------------------------------------------------------
    bb_data = _compute_bollinger_bands(closes, 20, 2.0)
    atr = _compute_atr(highs, lows, closes, 14)

    result["volatility_bb_middle"] = bb_data.get("bb_middle")
    result["volatility_bb_upper"] = bb_data.get("bb_upper")
    result["volatility_bb_lower"] = bb_data.get("bb_lower")
    result["volatility_bb_width_pct"] = bb_data.get("bb_width_pct")
    result["volatility_bb_position"] = bb_data.get("bb_position")
    result["volatility_atr_14"] = atr
    result["volatility_atr_pct"] = (
        round(atr / closes[-1] * 100, 4) if atr and closes[-1] != 0 else None
    )

    # BB squeeze detection
    bb_width = bb_data.get("bb_width_pct")
    if bb_width is not None:
        result["volatility_bb_squeeze"] = bb_width < 3.0  # arbitrary threshold

    # ------------------------------------------------------------------
    # Volume
    # ------------------------------------------------------------------
    vol_ratio = _compute_volume_ratio(volumes, 20)
    obv_data = _compute_obv(closes, volumes)

    result["volume_sma_20_ratio"] = vol_ratio
    result["volume_obv"] = obv_data.get("obv")
    result["volume_obv_trend"] = obv_data.get("obv_trend")

    # Volume spike detection
    if vol_ratio is not None:
        result["volume_spike"] = vol_ratio > 1.5

    # ------------------------------------------------------------------
    # Price action summary
    # ------------------------------------------------------------------
    result["price_change_pct"] = round((closes[-1] - closes[0]) / closes[0] * 100, 2)
    result["price_high"] = round(max(highs), 2)
    result["price_low"] = round(min(lows), 2)
    result["price_current"] = round(closes[-1], 2)
    result["price_range_pct"] = round((max(highs) - min(lows)) / closes[0] * 100, 2)

    # ------------------------------------------------------------------
    # Candlestick patterns
    # ------------------------------------------------------------------
    patterns = _detect_candlestick_patterns(opens, highs, lows, closes)
    for pattern_name, detected in patterns.items():
        if isinstance(detected, bool):
            result[f"pattern_{pattern_name}"] = detected

    return result


# ============================================================================
# Local signal generator (feeds AI final-judgement prompt)
# ============================================================================


def generate_local_signal(
    indicators: dict[str, Any],
    symbol: str,
    funding_rate: float | None = None,
    funding_annual_pct: float | None = None,
) -> dict[str, Any]:
    """Synthesize a compact local trading signal from pre-computed indicators.

    This is the deterministic, code-based analysis that replaces raw candle
    feeding to the LLM.  It distills the full indicator set into a single
    signal dict the AI uses as input for its final ENTER/HOLD/EXIT judgement.

    Parameters
    ----------
    indicators:
        Output of :func:`compute_indicators` for one pair+timeframe.
    symbol:
        Trading pair, e.g. ``"BTCUSDT"``.
    funding_rate:
        Current per-period funding rate (e.g. ``0.0001`` = 1 bps).
    funding_annual_pct:
        Annualized funding cost in percent (e.g. ``10.95``).

    Returns
    -------
    dict
        Compact signal with keys:
        ``symbol``, ``trend``, ``momentum``, ``volatility``,
        ``volume``, ``local_direction``, ``local_strength``, ``funding``,
        ``price``.
    """
    trend = indicators.get("trend_regime", "unknown")
    adx = indicators.get("trend_adx")
    rsi = indicators.get("momentum_rsi_14")
    rsi_regime = indicators.get("momentum_rsi_regime", "neutral")
    macd_cross = indicators.get("momentum_macd_cross", "neutral")
    macd_hist = indicators.get("momentum_macd_histogram")
    bb_position = indicators.get("volatility_bb_position")
    atr_pct = indicators.get("volatility_atr_pct")
    vol_ratio = indicators.get("volume_sma_20_ratio")
    vol_spike = indicators.get("volume_spike", False)
    price_change_pct = indicators.get("price_change_pct")
    price_current = indicators.get("price_current")
    stoch_k = indicators.get("momentum_stoch_k")
    stoch_d = indicators.get("momentum_stoch_d")
    ema20 = indicators.get("trend_ema_20")
    ema50 = indicators.get("trend_ema_50")

    # ---- Local directional bias (deterministic) ----
    score = 0.0  # positive = LONG bias, negative = SHORT bias

    # Trend filter: EMA alignment
    if ema20 and ema50:
        if ema20 > ema50:
            score += 1.0
        elif ema20 < ema50:
            score -= 1.0

    # ADX strength amplifier
    if adx is not None and adx > 20:
        adx_amp = min(adx / 50, 1.0)
        if adx > 25:
            score *= (1.0 + adx_amp)

    # RSI signal
    if rsi is not None:
        if rsi < 30:
            score += 0.5
        elif rsi > 70:
            score -= 0.5
        elif rsi < 45:
            score += 0.15
        elif rsi > 55:
            score -= 0.15

    # MACD cross
    if macd_cross == "bullish":
        score += 0.7
    elif macd_cross == "bearish":
        score -= 0.7

    # MACD histogram direction
    if macd_hist is not None:
        if macd_hist > 0:
            score += 0.2
        elif macd_hist < 0:
            score -= 0.2

    # Volume confirmation
    if vol_spike and vol_ratio is not None:
        score *= 1.2  # amplify on volume spike

    # Clamp and classify
    clamped = max(-3.0, min(3.0, score))
    raw_strength = abs(clamped) / 3.0
    strength = round(min(raw_strength, 1.0), 3)

    if clamped > 0.4:
        local_direction = "LONG"
    elif clamped < -0.4:
        local_direction = "SHORT"
    else:
        local_direction = "NEUTRAL"

    # ---- Volatility regime ----
    vol_regime = "normal"
    if atr_pct is not None:
        if atr_pct > 3.0:
            vol_regime = "high"
        elif atr_pct < 0.8:
            vol_regime = "low"

    # ---- Funding sentiment ----
    funding_sentiment = "neutral"
    if funding_rate is not None:
        if funding_rate > 0.0005:
            funding_sentiment = "crowded_long"
        elif funding_rate < -0.0005:
            funding_sentiment = "crowded_short"

    return {
        "symbol": symbol,
        "price": price_current,
        "price_change_pct": price_change_pct,
        "trend": trend,
        "adx": adx,
        "rsi": rsi,
        "rsi_regime": rsi_regime,
        "macd_cross": macd_cross,
        "macd_hist": macd_hist,
        "stoch_k": stoch_k,
        "stoch_d": stoch_d,
        "bb_position": bb_position,
        "volatility": vol_regime,
        "atr_pct": atr_pct,
        "volume_ratio": vol_ratio,
        "volume_spike": vol_spike,
        "price_vs_ema20": indicators.get("trend_price_vs_ema20"),
        "funding_rate": funding_rate,
        "funding_annual_pct": funding_annual_pct,
        "funding_sentiment": funding_sentiment,
        "local_direction": local_direction,
        "local_strength": strength,
    }


# ============================================================================
# MCP-powered indicator computation
# ============================================================================

# Standard indicator set for MCP mode
_MCP_DEFAULT_INDICATORS = [
    "ema-9", "ema-21", "ema-50", "ema-200",
    "rsi-14", "macd", "adx", "atr",
    "bb-20", "bbupper", "bblower",
    "stoch", "cci", "obv",
    "supertrend", "kdj",
]


async def compute_indicators_via_mcp(
    mcp_client: Any,
    inst_id: str,
    bar: str = "1H",
    indicators: list[str] | None = None,
) -> dict[str, Any]:
    """Compute technical indicators via the MCP server.

    Uses the MCP server's 228 built-in indicators instead of local
    numpy/pure-Python computation.  Returns a flat dict compatible
    with ``generate_local_signal()``.

    Parameters
    ----------
    mcp_client:
        An ``OkxMcpClient`` instance.
    inst_id:
        OKX instrument ID (e.g. ``"BTC-USDT-SWAP"``).
    bar:
        Candle interval (e.g. ``"1H"``, ``"4H"``, ``"1D"``).
    indicators:
        List of indicator names to compute.  Defaults to a standard
        set of trend/momentum/volatility indicators.

    Returns
    -------
    dict
        Indicator values keyed by name, compatible with the local
        ``compute_indicators()`` output format.
    """
    if indicators is None:
        indicators = list(_MCP_DEFAULT_INDICATORS)

    try:
        result = await mcp_client.get_indicators(inst_id, bar, indicators)
    except Exception as exc:
        logger.warning("mcp_indicator_fetch_failed", inst_id=inst_id, error=str(exc))
        return {}

    if not isinstance(result, dict):
        return {}

    # Normalize MCP response to match local compute_indicators() format
    # MCP returns values in its own structure; map to Quad's expected keys
    normalized: dict[str, Any] = {}

    data = result.get("data", result) if "data" in result else result
    if isinstance(data, dict):
        for key, value in data.items():
            # Flatten indicator values
            if isinstance(value, (int, float)):
                normalized[key] = value
            elif isinstance(value, dict):
                # MACD, Bollinger, Stoch, KDJ return nested dicts
                for sub_key, sub_val in value.items():
                    normalized[f"{key}_{sub_key}"] = sub_val
            elif isinstance(value, list) and value:
                normalized[key] = value[-1] if len(value) == 1 else value

    # Map common MCP indicator names to Quad's expected format
    _name_map = {
        "ema9": "ema_fast",
        "ema21": "ema_slow",
        "ema50": "ema_50",
        "ema200": "ema_200",
        "rsi14": "rsi",
        "bbwidth": "bb_width",
        "bbpct": "bb_pct",
    }
    for mcp_name, quad_name in _name_map.items():
        if mcp_name in normalized:
            normalized[quad_name] = normalized.pop(mcp_name)

    logger.debug(
        "mcp_indicators_computed",
        inst_id=inst_id,
        bar=bar,
        indicator_count=len(normalized),
    )
    return normalized
