"""Exchange-related types for Quad options trading bot.

This module defines types for exchange account updates
and other exchange-specific data structures.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from quad.types.domain import Account


__all__ = [
    "AccountUpdate",
]


@dataclass
class AccountUpdate:
    """Represents an update to the account state from an exchange.

    This is typically received via a user data WebSocket stream
    or polled from the REST API.
    """

    account: Account
    """Updated account snapshot."""

    event_type: str = ""
    """Type of update event, e.g. 'ACCOUNT_UPDATE'."""

    timestamp: int = 0
    """Event timestamp in unix milliseconds."""
