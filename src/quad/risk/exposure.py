"""Futures position tracker for monitoring portfolio exposure.

Tracks per-symbol notional exposure, average leverage, liquidation proximity,
margin utilization, and funding rate snapshots across all futures positions.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog

from quad.types.strategy import StrategyContext


class FuturesPositionTracker:
    """Tracks and reports futures position exposure metrics.

    Monitors total notional exposure per symbol, aggregated leverage,
    liquidation risk, margin utilization, and funding rate history.

    Parameters
    ----------
    config:
        Configuration dictionary. The risk sub-section is extracted
        automatically.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._log = structlog.get_logger(__name__)
        self._log.info("futures_position_tracker_init")

        self._cfg: dict[str, Any] = config["risk"]

        # Cached exposure data
        self._notional_exposure: dict[str, Decimal] = {}
        self._avg_leverage: Decimal = Decimal(0)
        self._liquidation_risk: list[dict[str, Any]] = []
        self._margin_utilization: Decimal = Decimal(0)
        self._funding_rate_snapshots: dict[str, list[Decimal]] = {}
        self._last_report: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def compute_exposure(
        self,
        context: StrategyContext,
    ) -> dict[str, Any]:
        """Compute portfolio futures exposure from strategy context.

        Parameters
        ----------
        context:
            Current strategy execution context with futures_positions,
            funding_rates, and account data.

        Returns
        -------
        dict[str, Any]
            Exposure report with per-symbol notional exposure, average
            leverage, liquidation proximity, margin utilization, and
            funding rate data.
        """
        positions = context.futures_positions or []
        funding_rates = context.funding_rates or {}
        account = context.account

        # ------------------------------------------------------------------
        # 1. Per-symbol notional exposure
        # ------------------------------------------------------------------
        notional_per_symbol: dict[str, Decimal] = {}
        total_notional = Decimal(0)
        total_margin = Decimal(0)

        for pos in positions:
            symbol = pos.symbol
            size = Decimal(str(abs(pos.size)))
            mark_price = Decimal(str(pos.mark_price))
            notional = size * mark_price
            notional_per_symbol[symbol] = (
                notional_per_symbol.get(symbol, Decimal(0)) + notional
            )
            total_notional += notional
            total_margin += Decimal(str(pos.margin))

        self._notional_exposure = notional_per_symbol

        # ------------------------------------------------------------------
        # 2. Aggregated leverage
        # ------------------------------------------------------------------
        if account and account.total_wallet_balance > Decimal(0):
            self._avg_leverage = (
                total_notional / account.total_wallet_balance
            ).quantize(Decimal("0.01"))
        else:
            self._avg_leverage = Decimal(0)

        # ------------------------------------------------------------------
        # 3. Liquidation monitoring
        # ------------------------------------------------------------------
        liquidation_risk: list[dict[str, Any]] = []
        for pos in positions:
            if pos.liquidation_price > 0 and pos.mark_price > 0:
                if pos.position_side.value == "long":
                    distance = (
                        (pos.mark_price - pos.liquidation_price) / pos.mark_price
                    )
                else:
                    distance = (
                        (pos.liquidation_price - pos.mark_price) / pos.mark_price
                    )
                liquidation_risk.append({
                    "symbol": pos.symbol,
                    "side": pos.position_side.value,
                    "distance_to_liquidation_pct": round(distance * 100, 4),
                    "liquidation_price": pos.liquidation_price,
                    "mark_price": pos.mark_price,
                })
        self._liquidation_risk = liquidation_risk

        # ------------------------------------------------------------------
        # 4. Margin utilization
        # ------------------------------------------------------------------
        if account and account.total_wallet_balance > Decimal(0):
            self._margin_utilization = (
                total_margin / account.total_wallet_balance
            ).quantize(Decimal("0.0001"))
        else:
            self._margin_utilization = Decimal(0)

        # ------------------------------------------------------------------
        # 5. Funding rate tracking
        # ------------------------------------------------------------------
        symbol_funding: dict[str, Decimal] = {}
        for sym, fr in funding_rates.items():
            rate_dec = Decimal(str(fr.funding_rate))
            self._funding_rate_snapshots.setdefault(sym, []).append(rate_dec)
            # Keep only the last 100 snapshots
            if len(self._funding_rate_snapshots[sym]) > 100:
                self._funding_rate_snapshots[sym] = (
                    self._funding_rate_snapshots[sym][-100:]
                )
            symbol_funding[sym] = rate_dec

        # ------------------------------------------------------------------
        # Build report
        # ------------------------------------------------------------------
        report: dict[str, Any] = {
            "notional_exposure": {
                k: str(v) for k, v in notional_per_symbol.items()
            },
            "total_notional_usd": str(total_notional.quantize(Decimal("0.01"))),
            "avg_leverage": str(self._avg_leverage),
            "margin_utilization_pct": str(
                (self._margin_utilization * Decimal(100)).quantize(Decimal("0.01"))
            ),
            "liquidation_risk": liquidation_risk,
            "num_positions": len(positions),
            "funding_rates": {k: str(v) for k, v in symbol_funding.items()},
        }

        self._last_report = report
        return report

    def get_exposure_report(self) -> dict[str, Any]:
        """Return the latest computed exposure report.

        Returns an empty-structured report if no data has been computed yet.
        """
        if self._last_report:
            return dict(self._last_report)

        return {
            "notional_exposure": {},
            "total_notional_usd": "0",
            "avg_leverage": "0",
            "margin_utilization_pct": "0",
            "liquidation_risk": [],
            "num_positions": 0,
            "funding_rates": {},
        }
