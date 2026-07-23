"""Backtest engine for Quad options trading bot.

Simulates running a trading strategy against historical option data.
The engine steps through time, evaluates the strategy at each interval,
and tracks simulated trades, PnL, and performance metrics without
placing real orders.
"""

from __future__ import annotations

import math
import statistics
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import structlog

from quad.types.domain import Trade
from quad.types.risk import Action
from quad.types.strategy import StrategyContext

from .models import BacktestResult, EquityPoint

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG: dict[str, Any] = {
    "starting_capital": Decimal("10000"),
    "commission_pct": Decimal("0.001"),  # 0.1% per trade
    "slippage_pct": Decimal("0.0005"),  # 0.05% slippage per fill
    "max_trades_per_day": 10,
}


# ============================================================================
# BacktestEngine
# ============================================================================


class BacktestEngine:
    """Simulates strategy execution against historical data.

    Loads historical option snapshots from the database, steps through
    time, evaluates the strategy at each interval, and tracks simulated
    trades and performance metrics.  Does NOT place real orders.

    Parameters
    ----------
    strategy:
        The strategy instance to evaluate.  Must implement
        ``evaluate(context) -> list[Action]``.
    db_manager:
        Database manager for loading historical data.  If ``None``,
        the engine runs in stub mode.
    config:
        Optional configuration overrides.  See ``_DEFAULT_CONFIG`` for
        available keys.
    """

    def __init__(
        self,
        strategy: Any,
        db_manager: Any = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._log = logger.bind(
            strategy=getattr(strategy, "get_name", lambda: "unknown")()
            if strategy
            else "none",
        )
        self._strategy = strategy
        self._db = db_manager

        merged = dict(_DEFAULT_CONFIG)
        if config:
            merged.update(config)
        self._config = merged

        # Simulation state
        self._starting_capital: Decimal = self._config["starting_capital"]
        self._commission_pct: Decimal = self._config["commission_pct"]
        self._slippage_pct: Decimal = self._config["slippage_pct"]
        self._max_trades_per_day: int = self._config["max_trades_per_day"]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        underlying: str,
        start: datetime,
        end: datetime,
        interval_hours: int = 1,
    ) -> BacktestResult:
        """Run a backtest simulation over the specified period.

        Steps through time at ``interval_hours`` granularity, calling the
        strategy's ``evaluate()`` at each step.  Simulates fills, tracks
        trades, and calculates performance metrics.

        Parameters
        ----------
        underlying:
            Underlying asset symbol (e.g. ``'BTCUSDT'``).
        start:
            Start of the simulation period.
        end:
            End of the simulation period.
        interval_hours:
            Number of hours between each evaluation step (default 1).

        Returns
        -------
        BacktestResult
            Complete backtest result with performance metrics, trades,
            and equity curve.
        """
        if self._strategy is None:
            msg = "No strategy provided to backtest engine. Cannot run simulation."
            self._log.error("backtest_no_strategy")
            raise ValueError(msg)

        strategy_name = self._strategy.get_name()
        self._log.info(
            "backtest_starting",
            strategy=strategy_name,
            underlying=underlying,
            start=start.isoformat(),
            end=end.isoformat(),
            interval_hours=interval_hours,
        )

        total_days = max(1, (end - start).days)
        simulated_trades: list[Trade] = []
        simulated_actions: list[Action] = []
        equity_curve: list[EquityPoint] = []

        # Simulation state
        cash: Decimal = self._starting_capital
        peak_equity: Decimal = self._starting_capital
        max_drawdown: Decimal = Decimal("0")
        gross_wins: Decimal = Decimal("0")
        gross_losses: Decimal = Decimal("0")

        trade_id_counter = 0
        open_positions: dict[str, dict[str, Any]] = {}

        current = start
        step_count = 0

        while current < end:
            step_count += 1
            ts_ms = int(current.timestamp() * 1000)

            # Build strategy context from historical data
            context = await self._build_context(
                underlying=underlying,
                timestamp=current,
                timestamp_ms=ts_ms,
                open_positions=open_positions,
            )

            # Evaluate strategy
            try:
                actions = await self._strategy.evaluate(context)
            except Exception as exc:
                self._log.warning(
                    "backtest_evaluate_error",
                    step=step_count,
                    timestamp=current.isoformat(),
                    error=str(exc),
                )
                current += timedelta(hours=interval_hours)
                continue

            if not actions:
                current += timedelta(hours=interval_hours)
                continue

            simulated_actions.extend(actions)

            # Process each action
            for action in actions:
                if action.type == "HOLD":
                    continue

                if action.type == "ENTER":
                    trade_id_counter += 1
                    trade = self._simulate_fill(
                        action=action,
                        trade_id=trade_id_counter,
                        timestamp=ts_ms,
                        context=context,
                    )
                    if trade is not None:
                        simulated_trades.append(trade)
                        open_positions[trade.symbol] = {
                            "entry_price": trade.price,
                            "quantity": trade.quantity,
                            "side": trade.side,
                            "entry_timestamp": ts_ms,
                        }
                        cash -= trade.price * trade.quantity + trade.fee
                        if trade.pnl > 0:
                            gross_wins += trade.pnl
                            gross_losses += Decimal("0")
                        else:
                            gross_losses += abs(trade.pnl)
                            gross_wins += Decimal("0")

                elif action.type == "EXIT":
                    pos = open_positions.pop(action.contract or "", None)
                    if pos is not None:
                        trade_id_counter += 1
                        exit_price = (action.price or Decimal("0"))
                        entry_price = pos["entry_price"]
                        quantity = pos["quantity"]
                        pnl = (exit_price - entry_price) * quantity
                        commission = entry_price * quantity * self._commission_pct

                        trade = Trade(
                            id=trade_id_counter,
                            symbol=action.contract or "",
                            side=action.side or "SELL",
                            quantity=quantity,
                            price=exit_price,
                            fee=commission,
                            pnl=pnl,
                            timestamp=ts_ms,
                        )
                        simulated_trades.append(trade)
                        cash += exit_price * quantity - commission

                        if pnl > 0:
                            gross_wins += pnl
                        else:
                            gross_losses += abs(pnl)

            # Update equity curve
            positions_value = self._compute_positions_value(
                open_positions=open_positions,
                context=context,
            )
            total_equity = cash + positions_value
            drawdown = (peak_equity - total_equity) / peak_equity if peak_equity > 0 else Decimal("0")

            if total_equity > peak_equity:
                peak_equity = total_equity

            if drawdown > max_drawdown:
                max_drawdown = drawdown

            equity_curve.append(
                EquityPoint(
                    timestamp=current,
                    equity=total_equity,
                    drawdown=drawdown,
                )
            )

            current += timedelta(hours=interval_hours)

        # Calculate performance metrics
        total_trades = len(simulated_trades)
        winning_trades = [t for t in simulated_trades if t.pnl > 0]
        losing_trades = [t for t in simulated_trades if t.pnl <= 0]

        win_count = len(winning_trades)
        loss_count = len(losing_trades)
        win_rate = win_count / total_trades if total_trades > 0 else 0.0

        total_pnl = sum((t.pnl for t in simulated_trades), Decimal("0"))
        total_commission = sum((t.fee for t in simulated_trades), Decimal("0"))

        avg_win = (
            sum((t.pnl for t in winning_trades), Decimal("0")) / win_count
            if win_count > 0
            else Decimal("0")
        )
        avg_loss = (
            sum((t.pnl for t in losing_trades), Decimal("0")) / loss_count
            if loss_count > 0
            else Decimal("0")
        )

        profit_factor = float(gross_wins / gross_losses) if gross_losses > 0 else float("inf")
        return_pct = (total_pnl / self._starting_capital) * 100 if self._starting_capital > 0 else Decimal("0")
        annualised = (
            return_pct * (Decimal("365") / Decimal(str(total_days)))
            if total_days > 0
            else Decimal("0")
        )

        # Sharpe ratio (if we have enough data)
        sharpe_ratio = self._calculate_sharpe(equity_curve, interval_hours)

        result = BacktestResult(
            strategy_name=strategy_name,
            underlying=underlying,
            start=start,
            end=end,
            total_trades=total_trades,
            winning_trades=win_count,
            losing_trades=loss_count,
            win_rate=win_rate,
            total_pnl=total_pnl,
            max_drawdown=max_drawdown,
            sharpe_ratio=sharpe_ratio,
            avg_win=avg_win,
            avg_loss=avg_loss,
            profit_factor=profit_factor,
            total_commission=total_commission,
            trades=simulated_trades,
            equity_curve=equity_curve,
            actions=simulated_actions,
            return_pct=return_pct,
            annualised_return_pct=annualised,
            total_days=total_days,
        )

        self._log.info(
            "backtest_complete",
            total_trades=total_trades,
            win_rate=win_rate,
            total_pnl=str(total_pnl),
            max_drawdown=str(max_drawdown),
            sharpe=sharpe_ratio,
        )

        return result

    # ------------------------------------------------------------------
    # Internal: context building
    # ------------------------------------------------------------------

    async def _build_context(
        self,
        underlying: str,
        timestamp: datetime,
        timestamp_ms: int,
        open_positions: dict[str, dict[str, Any]],
    ) -> StrategyContext:
        """Build a ``StrategyContext`` from historical data at a given timestamp.

        Attempts to load option chain snapshots and candle data from the
        database.  Falls back to empty data if the database is unavailable.
        """
        context = StrategyContext(
            config=dict(self._config),
            strategy_params={},
        )

        if self._db is not None:
            try:
                # Try to get historical option chain snapshot
                async with self._db.pool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT * FROM option_contracts WHERE underlying = $1 AND expiry > $2",
                        underlying, timestamp_ms,
                    )
                    context.option_chain = list(rows or [])
            except Exception as exc:
                self._log.debug(
                    "backtest_chain_load_failed",
                    timestamp=timestamp.isoformat(),
                    error=str(exc),
                )

        return context

    # ------------------------------------------------------------------
    # Internal: fill simulation
    # ------------------------------------------------------------------

    def _simulate_fill(
        self,
        action: Action,
        trade_id: int,
        timestamp: int,
        context: StrategyContext,
    ) -> Trade | None:
        """Simulate a fill for the given action at the current market price.

        Applies slippage to the theoretical fill price.  Returns ``None``
        if the action cannot be filled (e.g. missing price data).
        """
        if action.contract is None:
            return None

        # Determine fill price from context or action price
        fill_price = action.price

        if fill_price is None:
            # Try to find a price from the option chain
            for contract in context.option_chain:
                symbol = getattr(contract, "symbol", getattr(contract, "contract", ""))
                if symbol == action.contract:
                    mid = (
                        (Decimal(str(getattr(contract, "bid", 0) or 0))
                         + Decimal(str(getattr(contract, "ask", 0) or 0)))
                        / Decimal("2")
                    )
                    if mid > 0:
                        fill_price = mid
                    break

        if fill_price is None or fill_price <= 0:
            self._log.debug(
                "backtest_no_fill_price",
                contract=action.contract,
                action_type=action.type,
            )
            return None

        # Apply slippage
        slippage = fill_price * self._slippage_pct
        if action.side == "BUY":
            fill_price += slippage
        else:
            fill_price -= slippage

        fill_price = max(fill_price, Decimal("0"))

        # Calculate commission
        commission = fill_price * action.quantity * self._commission_pct

        trade = Trade(
            id=trade_id,
            symbol=action.contract,
            side=action.side or "BUY",
            quantity=action.quantity,
            price=fill_price,
            fee=commission,
            pnl=Decimal("0"),  # Realised PnL calculated on EXIT
            timestamp=timestamp,
        )

        return trade

    # ------------------------------------------------------------------
    # Internal: position value
    # ------------------------------------------------------------------

    def _compute_positions_value(
        self,
        open_positions: dict[str, dict[str, Any]],
        context: StrategyContext,
    ) -> Decimal:
        """Compute the current market value of all open positions."""
        total = Decimal("0")

        for symbol, pos in open_positions.items():
            current_price = pos.get("entry_price", Decimal("0"))
            # Try to get a current price from the context
            for contract in context.option_chain:
                sym = getattr(contract, "symbol", getattr(contract, "contract", ""))
                if sym == symbol:
                    mid = (
                        (Decimal(str(getattr(contract, "bid", 0) or 0))
                         + Decimal(str(getattr(contract, "ask", 0) or 0)))
                        / Decimal("2")
                    )
                    if mid > 0:
                        current_price = mid
                    break

            total += current_price * pos.get("quantity", Decimal("0"))

        return total

    # ------------------------------------------------------------------
    # Internal: Sharpe ratio
    # ------------------------------------------------------------------

    @staticmethod
    def _calculate_sharpe(
        equity_curve: list[EquityPoint],
        interval_hours: int = 1,
    ) -> float:
        """Calculate the annualised Sharpe ratio from the equity curve.

        Annualises the period Sharpe using the actual interval between
        equity-curve points, so the result is always an annualised ratio
        regardless of ``interval_hours``.

        Assumes a risk-free rate of 0.  Returns 0.0 if insufficient data.
        """
        if len(equity_curve) < 5:
            return 0.0

        # Compute period returns
        returns: list[float] = []
        for i in range(1, len(equity_curve)):
            prev_equity = float(equity_curve[i - 1].equity)
            curr_equity = float(equity_curve[i].equity)
            if prev_equity > 0:
                period_return = (curr_equity - prev_equity) / prev_equity
                returns.append(period_return)

        if len(returns) < 2:
            return 0.0

        try:
            avg_return = statistics.mean(returns)
            std_dev = statistics.stdev(returns)
        except (statistics.StatisticsError, ValueError):
            return 0.0

        if std_dev == 0:
            return 0.0

        # Annualise: there are (365 * 24 / interval_hours) periods per year
        periods_per_year = (365 * 24) / max(interval_hours, 1)
        sharpe = (avg_return / std_dev) * math.sqrt(periods_per_year)

        return round(sharpe, 4)
