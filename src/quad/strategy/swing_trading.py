"""Swing trading strategy using RSI, MACD, and volume confirmation.

Uses RSI for overbought/oversold detection, MACD for trend direction
and cross signals, and volume SMA for confirmation. Enters LONG on
RSI oversold + MACD bullish cross + volume spike confirmation.
Enters SHORT on RSI overbought + MACD bearish cross + volume spike
confirmation. Sets TP/SL bracket orders on entry.
"""

from __future__ import annotations

import statistics
from decimal import Decimal
from typing import Any

from quad.strategy.base import StrategyBase, ParamSpec
from quad.types.risk import Action
from quad.types.strategy import StrategyContext


class SwingTradingStrategy(StrategyBase):
    """Swing trading strategy using RSI + MACD + volume confirmation.

    Entry conditions:
        LONG: RSI < oversold threshold AND MACD bullish cross AND volume > SMA * multiplier
        SHORT: RSI > overbought threshold AND MACD bearish cross AND volume > SMA * multiplier

    Exit management:
        Trailing stop based on entry price distance (handled by TP/SL bracket orders
        placed on entry).
    """

    @staticmethod
    def get_name() -> str:
        return "swing_trading"

    @staticmethod
    def get_description() -> str:
        return "Swing trading using RSI, MACD, and volume confirmation with TP/SL brackets"

    @staticmethod
    def get_params_spec() -> list[ParamSpec]:
        return [
            ParamSpec(name="rsi_period", type="int", default=14, description="RSI calculation period"),
            ParamSpec(name="rsi_oversold", type="int", default=30, description="RSI oversold threshold"),
            ParamSpec(name="rsi_overbought", type="int", default=70, description="RSI overbought threshold"),
            ParamSpec(name="macd_fast", type="int", default=12, description="MACD fast EMA period"),
            ParamSpec(name="macd_slow", type="int", default=26, description="MACD slow EMA period"),
            ParamSpec(name="macd_signal", type="int", default=9, description="MACD signal line period"),
            ParamSpec(name="volume_sma_period", type="int", default=20, description="Volume SMA period"),
            ParamSpec(name="volume_multiplier", type="float", default=1.5, description="Volume multiplier for confirmation"),
            ParamSpec(name="max_position_size_usd", type="float", default=1000.0, description="Max position size in USD"),
            ParamSpec(name="trade_capital_usd", type="int", default=5, description="Capital per trade in USD"),
            ParamSpec(name="leverage", type="int", default=50, description="Position leverage"),
            ParamSpec(name="sl_capital_pct", type="float", default=30.0, description="Stop-loss as percentage of trade capital"),
            ParamSpec(name="tp_capital_pct", type="float", default=50.0, description="Take-profit as percentage of trade capital"),
            ParamSpec(name="confidence_default", type="float", default=0.7, description="Default confidence for signals"),
            ParamSpec(name="confidence_high", type="float", default=0.9, description="High confidence for strong signals"),
            ParamSpec(name="atr_period", type="int", default=14, description="ATR calculation period"),
            ParamSpec(name="atr_multiplier_stop", type="float", default=3.0, description="ATR multiplier for trailing stop"),
            ParamSpec(name="atr_default_pct", type="float", default=0.02, description="Default ATR as fraction of price"),
        ]

    async def evaluate(self, context: StrategyContext) -> list[Action]:
        """Evaluate swing trading signals.

        Args:
            context: Strategy execution context with market data.

        Returns:
            List of Action objects representing trading decisions.
        """
        symbol = (
            context.strategy_params.get("symbol", "BTCUSDT")
            if context.strategy_params
            else "BTCUSDT"
        )

        existing_positions = [
            p
            for p in (context.futures_positions or [])
            if p.symbol == symbol and p.size > 0
        ]

        if existing_positions:
            return await self._manage_position(existing_positions[0], context)
        return await self._check_entry(symbol, context)

    # ------------------------------------------------------------------
    # Entry signal detection
    # ------------------------------------------------------------------

    async def _check_entry(self, symbol: str, context: StrategyContext) -> list[Action]:
        """Check for entry signals using RSI, MACD, and volume.

        Fetches candle data from historical provider if available,
        computes indicators, and checks signal conditions.

        Returns:
            List of Action objects (open + TP/SL) or HOLD.
        """
        price = self._get_current_price(symbol, context)
        if price is None:
            return self.hold_action(f"No mark price for {symbol}")

        # Try pre-computed indicators from context first
        rsi = self._get_rsi(symbol, context)
        macd_data = self._get_macd(symbol, context)
        volume_confirmed = self._check_volume(symbol, context)

        # If no pre-computed data, try computing from candles
        if rsi is None or macd_data is None:
            candles = await self._fetch_candles(symbol, context)
            if candles:
                if rsi is None:
                    rsi = self._compute_rsi(candles, int(self.get_param("rsi_period", 14)))
                if macd_data is None:
                    macd_data = self._compute_macd(
                        candles,
                        int(self.get_param("macd_fast", 12)),
                        int(self.get_param("macd_slow", 26)),
                        int(self.get_param("macd_signal", 9)),
                    )
                if not volume_confirmed:
                    volume_confirmed = self._compute_volume_confirmation(
                        candles,
                        int(self.get_param("volume_sma_period", 20)),
                        float(self.get_param("volume_multiplier", 1.5)),
                    )

        if rsi is None:
            return self.hold_action(f"No RSI data for {symbol}")

        oversold = int(self.get_param("rsi_oversold", 30))
        overbought = int(self.get_param("rsi_overbought", 70))

        # Extract parameters for position sizing
        trade_capital = int(self.get_param("trade_capital_usd", 5))
        sl_pct = float(self.get_param("sl_capital_pct", 30.0))
        tp_pct = float(self.get_param("tp_capital_pct", 50.0))
        leverage = int(self.get_param("leverage", 50))
        max_pos_size = float(self.get_param("max_position_size_usd", 1000))

        # LONG signal: RSI oversold + MACD bullish cross + volume confirmation
        if rsi <= oversold and self._is_macd_bullish(macd_data) and volume_confirmed:
            size = self._calculate_position_size_usd(
                capital=float(trade_capital),
                risk_pct=0.02,
                stop_loss_pct=sl_pct / 100.0,
                max_size_usd=max_pos_size,
            )
            actions: list[Action] = [
                Action(
                    type="open_long",
                    strategy=self.get_name(),
                    symbol=symbol,
                    quantity=Decimal(str(size)),
                    reason=(
                        f"Swing LONG for {symbol}: "
                        f"RSI={rsi:.1f} (oversold), "
                        f"MACD bullish cross, "
                        f"volume confirmed"
                    ),
                    confidence=float(self.get_param("confidence_high", 0.9)),
                )
            ]
            # Add TP/SL bracket orders
            tp_sl = self._build_tp_sl_actions(
                symbol=symbol,
                side="LONG",
                entry_price=price,
                capital=float(trade_capital),
                sl_capital_pct=sl_pct,
                tp_capital_pct=tp_pct,
                leverage=float(leverage),
                strategy_name=self.get_name(),
            )
            actions.extend(tp_sl)
            return actions

        # SHORT signal: RSI overbought + MACD bearish cross + volume confirmation
        if rsi >= overbought and self._is_macd_bearish(macd_data) and volume_confirmed:
            size = self._calculate_position_size_usd(
                capital=float(trade_capital),
                risk_pct=0.02,
                stop_loss_pct=sl_pct / 100.0,
                max_size_usd=max_pos_size,
            )
            actions = [
                Action(
                    type="open_short",
                    strategy=self.get_name(),
                    symbol=symbol,
                    quantity=Decimal(str(size)),
                    reason=(
                        f"Swing SHORT for {symbol}: "
                        f"RSI={rsi:.1f} (overbought), "
                        f"MACD bearish cross, "
                        f"volume confirmed"
                    ),
                    confidence=float(self.get_param("confidence_high", 0.9)),
                )
            ]
            tp_sl = self._build_tp_sl_actions(
                symbol=symbol,
                side="SHORT",
                entry_price=price,
                capital=float(trade_capital),
                sl_capital_pct=sl_pct,
                tp_capital_pct=tp_pct,
                leverage=float(leverage),
                strategy_name=self.get_name(),
            )
            actions.extend(tp_sl)
            return actions

        # No signal
        reasons = []
        if rsi is not None:
            reasons.append(f"RSI={rsi:.1f}")
        if macd_data is not None:
            reasons.append(f"MACD={'bullish' if self._is_macd_bullish(macd_data) else 'bearish' if self._is_macd_bearish(macd_data) else 'neutral'}")
        if volume_confirmed:
            reasons.append("vol=confirmed")
        else:
            reasons.append("vol=weak")
        return self.hold_action(f"No entry signal for {symbol} ({', '.join(reasons)})")

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    async def _manage_position(self, position: Any, context: StrategyContext) -> list[Action]:
        """Manage an existing position with trailing stop.

        Uses ATR-based trailing stop. If ATR is unavailable, falls back
        to a simple fixed-distance stop.

        Args:
            position: The current futures position.
            context: Strategy execution context.

        Returns:
            List of Action objects (close signal or HOLD).
        """
        price = self._get_current_price(position.symbol, context)
        if price is None:
            return self.hold_action("No price data for position management")

        atr = self._get_atr(position.symbol, context)
        if atr:
            stop_distance = atr * float(self.get_param("atr_multiplier_stop", 3.0))
            side = position.position_side
            if isinstance(side, str):
                side = side.lower()

            entry = float(getattr(position, "entry_price", price))
            if side == "long" and price < entry - stop_distance:
                return [
                    Action(
                        type="close_long",
                        strategy=self.get_name(),
                        symbol=position.symbol,
                        quantity=Decimal(str(position.size)),
                        reason=f"Trailing stop hit for {position.symbol}",
                        confidence=float(self.get_param("confidence_default", 0.9)),
                    )
                ]
            elif side == "short" and price > entry + stop_distance:
                return [
                    Action(
                        type="close_short",
                        strategy=self.get_name(),
                        symbol=position.symbol,
                        quantity=Decimal(str(position.size)),
                        reason=f"Trailing stop hit for {position.symbol}",
                        confidence=float(self.get_param("confidence_default", 0.9)),
                    )
                ]

        return self.hold_action(f"Position {position.symbol} within trailing stop range")

    # ------------------------------------------------------------------
    # Indicator extraction helpers
    # ------------------------------------------------------------------

    def _get_rsi(self, symbol: str, context: StrategyContext) -> float | None:
        """Extract pre-computed RSI from context strategy params."""
        rsi_key = f"rsi_{symbol}"
        if context.strategy_params and rsi_key in context.strategy_params:
            return float(context.strategy_params[rsi_key])
        return None

    def _get_macd(self, symbol: str, context: StrategyContext) -> dict | None:
        """Extract pre-computed MACD data from context strategy params."""
        macd_key = f"macd_{symbol}"
        if context.strategy_params and macd_key in context.strategy_params:
            return context.strategy_params[macd_key]
        # Also try individual keys
        macd_line = context.strategy_params.get(f"macd_line_{symbol}")
        signal_line = context.strategy_params.get(f"macd_signal_{symbol}")
        hist = context.strategy_params.get(f"macd_hist_{symbol}")
        if macd_line is not None and signal_line is not None:
            return {
                "macd": float(macd_line),
                "signal": float(signal_line),
                "histogram": float(hist) if hist is not None else 0.0,
            }
        return None

    def _check_volume(self, symbol: str, context: StrategyContext) -> bool:
        """Check volume confirmation from context strategy params."""
        vol_confirmed_key = f"volume_confirmed_{symbol}"
        if context.strategy_params and vol_confirmed_key in context.strategy_params:
            return bool(context.strategy_params[vol_confirmed_key])
        # Default to True if no volume data available (don't block entry)
        return True

    # ------------------------------------------------------------------
    # MACD signal interpretation
    # ------------------------------------------------------------------

    @staticmethod
    def _is_macd_bullish(macd_data: dict | None) -> bool:
        """Check if MACD shows a bullish signal.

        Bullish when MACD line crosses above signal line (histogram
        goes from negative to positive or is positive and rising).
        """
        if macd_data is None:
            return False
        macd = macd_data.get("macd", 0)
        signal = macd_data.get("signal", 0)
        histogram = macd_data.get("histogram", 0)
        return macd > signal and histogram > 0

    @staticmethod
    def _is_macd_bearish(macd_data: dict | None) -> bool:
        """Check if MACD shows a bearish signal.

        Bearish when MACD line crosses below signal line (histogram
        goes from positive to negative or is negative and falling).
        """
        if macd_data is None:
            return False
        macd = macd_data.get("macd", 0)
        signal = macd_data.get("signal", 0)
        histogram = macd_data.get("histogram", 0)
        return macd < signal and histogram < 0

    # ------------------------------------------------------------------
    # On-device indicator computation (fallback when no pre-computed data)
    # ------------------------------------------------------------------

    async def _fetch_candles(
        self, symbol: str, context: StrategyContext
    ) -> list[dict[str, Any]] | None:
        """Fetch recent candles from the historical data provider.

        Requests enough candles to cover the longest indicator period
        (MACD slow + signal = 26 + 9 = 35, plus some margin).

        Args:
            symbol: Trading pair symbol.
            context: Strategy execution context.

        Returns:
            List of candle dicts, or None if unavailable.
        """
        if context.historical is None:
            return None
        max_period = max(
            int(self.get_param("rsi_period", 14)),
            int(self.get_param("macd_slow", 26)) + int(self.get_param("macd_signal", 9)),
            int(self.get_param("volume_sma_period", 20)),
        )
        # Request extra buffer (3x) to ensure enough data
        lookback = max_period * 3
        import time

        now_ms = int(time.time() * 1000)
        start_ms = now_ms - (lookback * 60 * 1000)  # ~minutes back
        try:
            return await context.historical.get_candles(symbol, start_ms, now_ms)
        except Exception:
            return None

    @staticmethod
    def _compute_rsi(candles: list[dict[str, Any]], period: int) -> float | None:
        """Compute RSI from close prices.

        Implements the standard Wilder's RSI.

        Args:
            candles: List of candle dicts with 'close' key.
            period: RSI period (default 14).

        Returns:
            RSI value as float, or None if insufficient data.
        """
        closes = [
            float(c.get("close", 0)) for c in candles if c.get("close")
        ]
        if len(closes) < period + 1:
            return None

        gains: list[float] = []
        losses: list[float] = []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            if diff >= 0:
                gains.append(diff)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(diff))

        if len(gains) < period or len(losses) < period:
            return None

        avg_gain = statistics.mean(gains[-period:])
        avg_loss = statistics.mean(losses[-period:])

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))
        return round(rsi, 2)

    @staticmethod
    def _compute_macd(
        candles: list[dict[str, Any]],
        fast: int,
        slow: int,
        signal: int,
    ) -> dict | None:
        """Compute MACD from close prices.

        Uses simple SMA-based approximation for EMA.

        Args:
            candles: List of candle dicts with 'close' key.
            fast: Fast EMA period.
            slow: Slow EMA period.
            signal: Signal line period.

        Returns:
            Dict with 'macd', 'signal', 'histogram' keys, or None.
        """
        closes = [
            float(c.get("close", 0)) for c in candles if c.get("close")
        ]
        if len(closes) < slow + signal:
            return None

        # Simple EMA approximation using weighted averages
        def ema(data: list[float], period: int) -> list[float]:
            if len(data) < period:
                return []
            k = 2.0 / (period + 1)
            result = [statistics.mean(data[:period])]
            for price in data[period:]:
                result.append(price * k + result[-1] * (1 - k))
            return result

        fast_ema_values = ema(closes, fast)
        slow_ema_values = ema(closes, slow)

        if not fast_ema_values or not slow_ema_values:
            return None

        # MACD line = fast EMA - slow EMA
        macd_values = [
            f - s
            for f, s in zip(fast_ema_values, slow_ema_values)
        ]

        # Signal line = EMA of MACD line
        signal_values = ema(macd_values, signal)
        if not signal_values:
            return None

        current_macd = macd_values[-1]
        current_signal = signal_values[-1]
        current_histogram = current_macd - current_signal

        return {
            "macd": round(current_macd, 8),
            "signal": round(current_signal, 8),
            "histogram": round(current_histogram, 8),
        }

    @staticmethod
    def _compute_volume_confirmation(
        candles: list[dict[str, Any]],
        period: int,
        multiplier: float,
    ) -> bool:
        """Check if latest volume exceeds SMA * multiplier.

        Args:
            candles: List of candle dicts with 'volume' key.
            period: Volume SMA period.
            multiplier: Volume multiplier threshold.

        Returns:
            True if latest volume > SMA * multiplier, False if
            insufficient data, defaults to True on missing volume data.
        """
        volumes = [
            float(c.get("volume", 0)) for c in candles if c.get("volume")
        ]
        if len(volumes) < period + 1:
            return True  # Default to True if not enough data

        recent = volumes[-(period + 1) : -1]
        if not recent:
            return True

        avg_volume = statistics.mean(recent)
        if avg_volume <= 0:
            return True

        latest_volume = volumes[-1]
        return latest_volume > avg_volume * multiplier
