"""Exposure limiter for monitoring and enforcing portfolio Greek limits.

Tracks total delta, gamma, theta, and vega exposure across all positions
and enforces configured maximums per Greek.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import structlog

from quad.types.domain import Position
from quad.types.market import OptionContract


# Default exposure limits
_DEFAULTS: dict[str, Any] = {
    "max_delta": Decimal("100"),
    "max_theta": Decimal("-500"),
    "max_vega": Decimal("500"),
}

# Supported Greek keys
GREEK_KEYS = ["delta", "gamma", "theta", "vega"]


class ExposureLimiter:
    """Monitors and enforces portfolio Greek exposure limits.

    Computes total portfolio Greeks by summing each position's Greek
    contribution (position quantity * contract Greek value) and compares
    them against configured limits.

    Parameters
    ----------
    config:
        Configuration dictionary. The exposure sub-section is extracted
        from ``config.get('risk', {}).get('exposure', {})``.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._log = structlog.get_logger(__name__)
        self._log.info("exposure_limiter_init")

        raw = config.get("risk", config)
        self._cfg: dict[str, Any] = raw if isinstance(raw, dict) else config

        self._exposure_cfg: dict[str, Any] = self._cfg.get("exposure", {})

        # Cached exposure data
        self._last_exposure: dict[str, Decimal] = {
            k: Decimal("0") for k in GREEK_KEYS
        }
        self._last_report: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def compute_exposure(
        self,
        positions: list[Position],
        option_chain: list[OptionContract],
    ) -> dict[str, Decimal]:
        """Compute total portfolio Greek exposure.

        For each open position, finds the matching OptionContract by
        symbol and multiplies the position quantity by the contract
        Greek value. Results are summed across all positions.

        Parameters
        ----------
        positions:
            List of current positions.
        option_chain:
            List of option contracts with Greek values.

        Returns
        -------
        dict[str, Decimal]
            Mapping of Greek name to total exposure value.
        """
        # Build a lookup from contract symbol to OptionContract
        contract_map: dict[str, OptionContract] = {
            c.symbol: c for c in option_chain
        }

        exposure: dict[str, Decimal] = {
            "delta": Decimal("0"),
            "gamma": Decimal("0"),
            "theta": Decimal("0"),
            "vega": Decimal("0"),
        }

        for pos in positions:
            contract = contract_map.get(pos.contract_symbol)
            if contract is None:
                self._log.debug(
                    "contract_not_found_for_exposure",
                    symbol=pos.contract_symbol,
                )
                continue

            qty = pos.quantity
            exposure["delta"] += contract.delta * qty
            exposure["gamma"] += contract.gamma * qty
            exposure["theta"] += contract.theta * qty
            exposure["vega"] += contract.vega * qty

        # Round for consistency
        for key in exposure:
            exposure[key] = exposure[key].quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )

        self._last_exposure = dict(exposure)
        return exposure

    async def check_delta_limit(self, exposure: dict[str, Decimal]) -> bool:
        """Check if absolute portfolio delta is within the configured limit.

        Returns
        -------
        bool
            True if ``|delta| <= max_delta``.
        """
        limit = Decimal(
            str(
                self._exposure_cfg.get(
                    "max_delta", _DEFAULTS["max_delta"]
                )
            )
        )
        delta = exposure.get("delta", Decimal("0"))
        result = abs(delta) <= limit

        if not result:
            self._log.warning(
                "delta_limit_exceeded",
                delta=str(delta),
                limit=str(limit),
            )
        return result

    async def check_theta_limit(self, exposure: dict[str, Decimal]) -> bool:
        """Check if portfolio theta is within the configured limit.

        For short option positions theta is typically negative (time
        decay works in our favour). The limit is a negative number
        (e.g. -500), so the check ensures theta is not too positive
        (too much long premium).

        theta > max_theta means theta is *better* (more negative) and
        passes. theta < max_theta means theta is worse and fails.

        Returns
        -------
        bool
            True if ``theta >= max_theta`` (theta is within acceptable
            range).
        """
        limit = Decimal(
            str(
                self._exposure_cfg.get(
                    "max_theta", _DEFAULTS["max_theta"]
                )
            )
        )
        theta = exposure.get("theta", Decimal("0"))

        # We want theta to be >= limit (i.e., not worse than the limit)
        # For short premium strategies, theta is negative and more negative
        # (e.g. -1000) is more short premium.
        # Theta > max_theta (e.g. -400 > -500) means less short premium = okay
        # Theta < max_theta (e.g. -600 < -500) means more short premium = exceeds
        result = theta >= limit

        if not result:
            self._log.warning(
                "theta_limit_exceeded",
                theta=str(theta),
                limit=str(limit),
            )
        return result

    async def check_vega_limit(self, exposure: dict[str, Decimal]) -> bool:
        """Check if portfolio vega is within the configured limit.

        Returns
        -------
        bool
            True if ``|vega| <= max_vega``.
        """
        limit = Decimal(
            str(
                self._exposure_cfg.get(
                    "max_vega", _DEFAULTS["max_vega"]
                )
            )
        )
        vega = exposure.get("vega", Decimal("0"))
        result = abs(vega) <= limit

        if not result:
            self._log.warning(
                "vega_limit_exceeded",
                vega=str(vega),
                limit=str(limit),
            )
        return result

    def get_exposure_report(self) -> dict[str, Any]:
        """Return a full exposure report with current values and limits.

        The report includes all calculated Greeks, their configured
        limits, and whether each is within bounds.
        """
        delta_limit = Decimal(
            str(
                self._exposure_cfg.get(
                    "max_delta", _DEFAULTS["max_delta"]
                )
            )
        )
        theta_limit = Decimal(
            str(
                self._exposure_cfg.get(
                    "max_theta", _DEFAULTS["max_theta"]
                )
            )
        )
        vega_limit = Decimal(
            str(
                self._exposure_cfg.get(
                    "max_vega", _DEFAULTS["max_vega"]
                )
            )
        )

        exposure = self._last_exposure

        return {
            "exposure": {
                "delta": str(exposure.get("delta", Decimal("0"))),
                "gamma": str(exposure.get("gamma", Decimal("0"))),
                "theta": str(exposure.get("theta", Decimal("0"))),
                "vega": str(exposure.get("vega", Decimal("0"))),
            },
            "limits": {
                "max_delta": str(delta_limit),
                "max_theta": str(theta_limit),
                "max_vega": str(vega_limit),
            },
            "within_limits": {
                "delta": abs(exposure.get("delta", Decimal("0"))) <= delta_limit,
                "theta": exposure.get("theta", Decimal("0")) >= theta_limit,
                "vega": abs(exposure.get("vega", Decimal("0"))) <= vega_limit,
            },
            "exceeded_limits": self.exceeds_limits(exposure),
        }

    def exceeds_limits(
        self, exposure: dict[str, Decimal]
    ) -> list[str]:
        """Return a list of Greek names whose limits are exceeded.

        Parameters
        ----------
        exposure:
            Computed Greek exposure dict.

        Returns
        -------
        list[str]
            Names of Greeks exceeding configured limits (e.g. ``["delta"]``).
        """
        delta_limit = Decimal(
            str(
                self._exposure_cfg.get(
                    "max_delta", _DEFAULTS["max_delta"]
                )
            )
        )
        theta_limit = Decimal(
            str(
                self._exposure_cfg.get(
                    "max_theta", _DEFAULTS["max_theta"]
                )
            )
        )
        vega_limit = Decimal(
            str(
                self._exposure_cfg.get(
                    "max_vega", _DEFAULTS["max_vega"]
                )
            )
        )

        exceeded: list[str] = []

        if abs(exposure.get("delta", Decimal("0"))) > delta_limit:
            exceeded.append("delta")
        if exposure.get("theta", Decimal("0")) < theta_limit:
            exceeded.append("theta")
        if abs(exposure.get("vega", Decimal("0"))) > vega_limit:
            exceeded.append("vega")

        return exceeded
