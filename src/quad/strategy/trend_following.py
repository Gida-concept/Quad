"""Trend-following strategy using EMA crossover and ADX filter.

Enters LONG when fast EMA crosses above slow EMA with strong trend
(ADX > threshold). Enters SHORT on the reverse cross (fast EMA below
slow EMA with ADX > threshold). Uses ATR-based trailing stop for
exits and sets TP/SL bracket orders on entry.
"""

from __future__ import annotations

import statistics
from decimal import Decimal
from typing import Any

from quad.strategy.base import StrategyBase, ParamSpec
from quad.types.risk import Action
from quad.types.strategy import StrategyContext


class TrendFollowingStrategy(StrategyBase):
    """Trend-following strategy using EMA crossover with ADX filter.

    Entry conditions:
        LONG: fast EMA > slow EMA AND ADX > threshold
        SHORT: fast EMA < slow EMA AND ADX > threshold

    Exit management:
        ATR-based trailing stop + TP/SL bracket orders on entry.
    """

    @staticmethod
    def get_name() -> str:
        return "trend_following"

    @staticmethod
    def get_description() -> str:
        return "Trend following using EMA crossover and ADX filter with TP/SL brackets"

    @staticmethod
    def get_params_spec() -> list[ParamSpec]:
        return [
            ParamSpec(name="fast_ema", type="int", default=9, description="Fast EMA period"),
            ParamSpec(name="slow_ema", type="int", default=21, description="Slow EMA period"),
            ParamSpec(name="adx_period", type="int", default=14, description="ADX calculation period"),
            ParamSpec(name="adx_threshold", type="int", default=25, description="Minimum ADX for trend strength"),
            ParamSpec(name="atr_period", type="int", default=14, description="ATR calculation period"),
            ParamSpec(name="atr_multiplier_stop", type="float", default=3.0, description="ATR multiplier for trailing stop"),
            ParamSpec(name="atr_default_pct", type="float", default=0.02, description="Default ATR as fraction of price"),
            ParamSpec(name="trade_capital_usd", type="int", default=5, description="Capital per trade in USD"),
            ParamSpec(name="tp_capital_pct", type="float", default=50.0, description="Take-profit as percentage of trade capital"),
            ParamSpec(name="confidence_default", type="float", default=0.7, description="Default confidence for signals"),
            ParamSpec(name="confidence_high", type="float", default=0.9, description="High confidence for strong signals"),
        ]

    async def evaluate(self, context: StrategyContext) -> list[Action]:
        """Evaluate trend-following signals.

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

    async def _check_entry(self, symbol: str, context: StrategyContext) -> list[Action]:
        """Check for EMA crossover + ADX entry signals.

        Supports both LONG and SHORT entries.

        Returns:
            List of Action objects or HOLD.
        """
        price = self._get_current_price(symbol, context)
        if price is None:
            return self.hold_action(f"No mark price for {symbol}")

        # Try pre-computed indicators from context first
        ema_data = self._get_ema_data(symbol, context)
        adx = self._get_adx(symbol, context)

        # Fallback: compute from candles
        if ema_data is None or adx is None:
            candles = await self._fetch_candles(symbol, context)
            if candles:
                if ema_data is None:
                    ema_data = self._compute_ema_cross(
                        candles,
                        int(self.get_param("fast_ema", 9)),
                        int(self.get_param("slow_ema", 21)),
                    )
                if adx is None:
                    adx = self._compute_adx(
                        candles,
                        int(self.get_param("adx_period", 14)),
                    )

        if ema_data is None:
            return self.hold_action(f"No EMA data for {symbol}")
        if adx is None:
            return self.hold_action(f"No ADX data for {symbol}")

        adx_threshold = int(self.get_param("adx_threshold", 25))

        if adx < adx_threshold:
            return self.hold_action(
                f"No entry for {symbol}: trend too weak "
                f"(ADX={adx:.1f} < {adx_threshold})"
            )

        fast_ema = ema_data.get("fast", 0)
        slow_ema = ema_data.get("slow", 0)

        trade_capital = int(self.get_param("trade_capital_usd", 5))
        sl_pct = float(self._config.get("risk", {}).get("per_position_sl", {}).get("capital_pct", 30.0))
        tp_pct = float(self.get_param("tp_capital_pct", 50.0))
        leverage = int(self._config.get("trading", {}).get("leverage", 50))
        # Read absolute max position size from risk config (single source of truth)
        max_pos_size = float(self._config.get("risk", {}).get("max_position_size_usd", 10000))

        # LONG: fast EMA > slow EMA + strong trend
        if fast_ema > slow_ema:
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
                        f"Trend-following LONG for {symbol}: "
                        f"fast EMA ({fast_ema:.2f}) > slow EMA ({slow_ema:.2f}), "
                        f"ADX={adx:.1f}"
                    ),
                    confidence=float(self.get_param("confidence_high", 0.9)),
                )
            ]
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

        # SHORT: fast EMA < slow EMA + strong trend
        if fast_ema < slow_ema:
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
                        f"Trend-following SHORT for {symbol}: "
                        f"fast EMA ({fast_ema:.2f}) < slow EMA ({slow_ema:.2f}), "
                        f"ADX={adx:.1f}"
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

        return self.hold_action(
            f"No entry for {symbol}: EMAs converging "
            f"(fast={fast_ema:.2f}, slow={slow_ema:.2f})"
        )

    async def _manage_position(
        self, position: Any, context: StrategyContext
    ) -> list[Action]:
        """Manage an existing position with ATR-based trailing stop."""
        price = self._get_current_price(position.symbol, context)
        if price is None:
            return self.hold_action("No price data for position management")

        atr = self._get_atr(position.symbol, context)
        if atr:
            stop_distance = atr * float(
                self.get_param("atr_multiplier_stop", 3.0)
            )
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
                        confidence=float(
                            self.get_param("confidence_default", 0.9)
                        ),
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
                        confidence=float(
                            self.get_param("confidence_default", 0.9)
                        ),
                    )
                ]

        return self.hold_action(
            f"Position {position.symbol} within trailing stop range"
        )

    # ------------------------------------------------------------------
    # Indicator extraction helpers
    # ------------------------------------------------------------------

    def _get_ema_data(
        self, symbol: str, context: StrategyContext
    ) -> dict | None:
        """Extract pre-computed EMA values from context strategy params."""
        fast_key = f"ema_fast_{symbol}"
        slow_key = f"ema_slow_{symbol}"
        if context.strategy_params:
            fast = context.strategy_params.get(fast_key)
            slow = context.strategy_params.get(slow_key)
            if fast is not None and slow is not None:
                return {"fast": float(fast), "slow": float(slow)}
        return None

    def _get_adx(self, symbol: str, context: StrategyContext) -> float | None:
        """Extract pre-computed ADX from context strategy params."""
        adx_key = f"adx_{symbol}"
        if context.strategy_params and adx_key in context.strategy_params:
            return float(context.strategy_params[adx_key])
        return None

    # ------------------------------------------------------------------
    # On-device indicator computation (fallback)
    # ------------------------------------------------------------------

    async def _fetch_candles(
        self, symbol: str, context: StrategyContext
    ) -> list[dict[str, Any]] | None:
        """Fetch recent candles from the historical data provider.

        Requests enough candles to cover the longest indicator period.

        Args:
            symbol: Trading pair symbol.
            context: Strategy execution context.

        Returns:
            List of candle dicts, or None if unavailable.
        """
        if context.historical is None:
            return None
        max_period = max(
            int(self.get_param("slow_ema", 21)),
            int(self.get_param("adx_period", 14)),
        )
        lookback = max_period * 3
        import time

        now_ms = int(time.time() * 1000)
        start_ms = now_ms - (lookback * 60 * 1000)
        try:
            return await context.historical.get_candles(symbol, start_ms, now_ms)
        except Exception:
            return None

    @staticmethod
    def _compute_ema_cross(
        candles: list[dict[str, Any]], fast: int, slow: int
    ) -> dict | None:
        """Compute EMA crossover data from candles.

        Args:
            candles: List of candle dicts with 'close' key.
            fast: Fast EMA period.
            slow: Slow EMA period.

        Returns:
            Dict with 'fast' and 'slow' EMA values, or None.
        """
        closes = [
            float(c.get("close", 0)) for c in candles if c.get("close")
        ]
        if len(closes) < slow + 1:
            return None

        def ema(data: list[float], period: int) -> float:
            if len(data) < period:
                return 0.0
            k = 2.0 / (period + 1)
            ema_val = statistics.mean(data[:period])
            for price in data[period:]:
                ema_val = price * k + ema_val * (1 - k)
            return ema_val

        return {
            "fast": round(ema(closes, fast), 8),
            "slow": round(ema(closes, slow), 8),
        }

    @staticmethod
    def _compute_adx(
        candles: list[dict[str, Any]], period: int
    ) -> float | None:
        """Compute ADX (Average Directional Index) from candles.

        Uses a simplified approximation: average of +DI and -DI
        difference over the period.

        Args:
            candles: List of candle dicts with 'high', 'low', 'close'.
            period: ADX period (default 14).

        Returns:
            ADX value, or None if insufficient data.
        """
        highs = [
            float(c.get("high", 0)) for c in candles if c.get("high")
        ]
        lows = [
            float(c.get("low", 0)) for c in candles if c.get("low")
        ]
        closes = [
            float(c.get("close", 0)) for c in candles if c.get("close")
        ]

        if len(highs) < period + 1 or len(lows) < period + 1:
            return None

        # True Range
        tr_values: list[float] = []
        for i in range(1, len(closes)):
            hl = highs[i] - lows[i]
            hc = abs(highs[i] - closes[i - 1])
            lc = abs(lows[i] - closes[i - 1])
            tr_values.append(max(hl, hc, lc))

        if len(tr_values) < period:
            return None

        # Simplified ADX: scaled average directional movement
        # Use ATR as a proxy for trend strength
        atr = statistics.mean(tr_values[-period:])
        if atr <= 0:
            return None

        # Calculate directional movement
        dm_plus: list[float] = []
        dm_minus: list[float] = []
        for i in range(1, len(highs)):
            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]
            if up_move > down_move and up_move > 0:
                dm_plus.append(up_move)
                dm_minus.append(0)
            elif down_move > up_move and down_move > 0:
                dm_plus.append(0)
                dm_minus.append(down_move)
            else:
                dm_plus.append(0)
                dm_minus.append(0)

        if len(dm_plus) < period:
            return None

        avg_dm_plus = statistics.mean(dm_plus[-period:])
        avg_dm_minus = statistics.mean(dm_minus[-period:])

        di_plus = 100.0 * avg_dm_plus / atr if atr > 0 else 0
        di_minus = 100.0 * avg_dm_minus / atr if atr > 0 else 0

        dx = 100.0 * abs(di_plus - di_minus) / (di_plus + di_minus) if (di_plus + di_minus) > 0 else 0
        adx = statistics.mean([dx] * 1)  # Simplified: single-period ADX

        return round(adx, 2)
