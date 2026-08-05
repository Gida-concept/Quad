"""TradingView webhook integration for Quad.

Receives and processes TradingView alert webhooks, converts them into
trading signals the bot can act on, and provides AI-driven chart analysis.

Exports
-------
parse_alert
    Parse a TradingView webhook payload into a structured dict.
convert_to_action
    Convert a parsed TradingView alert into an ``Action``-like dict.
TradingViewWebhook
    aiohttp handler that receives, validates, and routes incoming alerts.
"""

from __future__ import annotations

from .parser import parse_alert
from .signals import TradingViewSignal, convert_to_action

__all__ = [
    "TradingViewSignal",
    "convert_to_action",
    "parse_alert",
]
