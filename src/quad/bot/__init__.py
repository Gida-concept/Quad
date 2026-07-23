"""Quad Telegram bot — PRIMARY user interface.

The bot runs as a long-lived async polling process built on
python-telegram-bot v20+ Application pattern.  All user interaction
goes through Telegram commands and periodic job notifications.
"""

from __future__ import annotations

from .bot import QuadBot

__all__ = [
    "QuadBot",
]
