"""Groq AI integration for Quad options trading bot.

Provides AI-powered market analysis, strategy recommendations,
and trading decisions using Groq's ultra-fast LLM inference
(Llama models via the ``groq`` Python SDK).

Exports
-------
GroqClient
    Async wrapper around ``groq.AsyncGroq`` with rate-limit handling.
MarketContext
    Aggregated market snapshot dataclass for AI decision-making.
collect_market_context
    Fetch all market data (candles, positions, account, option chains).
compute_indicators
    Compute technical indicators from candle data.
build_trading_prompt
    Build structured system+user prompts for AI trading decisions.
analyze_market
    Analyze current market conditions from option chain data.
recommend_strategy
    Suggest a strategy based on market conditions.
"""

from __future__ import annotations

from .groq import GroqClient
from .analysis import analyze_market
from .strategist import recommend_strategy
from .context import MarketContext, collect_market_context
from .ta import compute_indicators
from .prompt import build_trading_prompt

__all__ = [
    "GroqClient",
    "MarketContext",
    "collect_market_context",
    "compute_indicators",
    "build_trading_prompt",
    "analyze_market",
    "recommend_strategy",
]
