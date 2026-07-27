"""Async Groq client wrapper for Quad.

Provides a production-ready wrapper around ``groq.AsyncGroq`` with:
- API key authentication (from parameter, env var, or config)
- Configurable model selection with sensible defaults
- Rate-limit awareness and automatic retry jitter
- Sliding-window rate limiter (max requests per day)
- Structured error handling with structlog logging
- Connection timeout and max-retry configuration

Usage
-----
.. code-block:: python

    client = GroqClient(api_key="...")
    response = await client.chat(
        system="You are a trading assistant.",
        user="Analyze BTC options chain...",
    )
    print(response)

    # Structured trading decision
    decision = await client.decide_trades(
        system_prompt="...",
        user_prompt="...",
    )
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from typing import Any

import structlog

from groq import AsyncGroq
from groq import (
    APIError,
    APIConnectionError,
    APIStatusError,
    RateLimitError,
)

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MODEL = "llama-3.3-70b-versatile"
"""Default model: fast, high-quality, 131K context, production-grade."""

_FALLBACK_MODEL = "llama-3.1-8b-instant"
"""Fallback model if the primary is unavailable or rate-limited."""

_DEFAULT_MAX_TOKENS = 1024
_DEFAULT_TEMPERATURE = 0.3


# ============================================================================
# GroqClient
# ============================================================================


class GroqClient:
    """Async wrapper around ``groq.AsyncGroq`` for Quad.

    Parameters
    ----------
    api_key:
        Groq API key.  If ``None`` (default), reads ``GROQ_API_KEY`` from
        the environment.
    model:
        Groq model ID to use for chat completions.
        Defaults to ``llama-3.3-70b-versatile``.
    timeout:
        Request timeout in seconds.
    max_retries:
        Maximum number of retries on rate-limit or transient errors.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        max_requests_per_day: int | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._config = config or {}
        self._ai_config = self._config["ai"]
        self._groq_config = self._ai_config["groq"]

        self._api_key = api_key or os.environ.get("GROQ_API_KEY", "")
        if not self._api_key:
            logger.warning("groq_api_key_missing")

        self._model = model or self._ai_config.get("model", _DEFAULT_MODEL)
        self._timeout = timeout or self._groq_config.get("timeout_seconds")
        self._max_retries = max_retries or self._groq_config.get("max_retries")

        # Rate limiter / backoff configuration
        rate_limiter_cfg = self._groq_config["rate_limiter"]
        self._max_requests_per_day = (
            max_requests_per_day
            or rate_limiter_cfg.get("max_requests_per_day")
        )
        self._rate_limit_window_s = rate_limiter_cfg.get("window_seconds")
        self._warning_level_1 = rate_limiter_cfg.get("warning_level_1")
        self._warning_level_2 = rate_limiter_cfg.get("warning_level_2")
        self._warning_level_3 = rate_limiter_cfg.get("warning_level_3")
        self._base_backoff = self._groq_config.get("base_backoff_seconds")

        self._log = logger.bind(model=self._model)

        # Internal async client (created eagerly to warm the connection)
        self._client: AsyncGroq = AsyncGroq(
            api_key=self._api_key,
            timeout=self._timeout,
            max_retries=0,  # We handle retries ourselves
        )

        # Retry / rate-limit stats
        self._total_requests: int = 0
        self._total_retries: int = 0
        self._last_rate_limit: float = 0.0

        # Sliding-window rate limiter: timestamps of requests in current window
        self._request_timestamps: deque[float] = deque()
        self._rate_limit_warning_sent: int = 0  # tracks highest warning level sent

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def model(self) -> str:
        """Currently configured model ID."""
        return self._model

    @property
    def stats(self) -> dict[str, Any]:
        """Return usage statistics for this client."""
        now = time.time()
        self._prune_timestamps(now)
        return {
            "model": self._model,
            "total_requests": self._total_requests,
            "total_retries": self._total_retries,
            "last_rate_limit": self._last_rate_limit,
            "requests_in_window": len(self._request_timestamps),
            "max_requests_per_day": self._max_requests_per_day,
            "available": self.is_available(),
        }

    # ------------------------------------------------------------------
    # Availability check
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Check if the client is available for trading decisions.

        Returns ``True`` if:
        - API key is configured, AND
        - The sliding-window count is below the daily limit.
        """
        if not self._api_key:
            return False
        now = time.time()
        self._prune_timestamps(now)
        return len(self._request_timestamps) < self._max_requests_per_day

    # ------------------------------------------------------------------
    # Rate limiter
    # ------------------------------------------------------------------

    def _prune_timestamps(self, now: float | None = None) -> None:
        """Remove timestamps outside the sliding window."""
        if now is None:
            now = time.time()
        cutoff = now - self._rate_limit_window_s
        while self._request_timestamps and self._request_timestamps[0] < cutoff:
            self._request_timestamps.popleft()

    async def _check_rate_limit(self) -> None:
        """Check rate limit and issue warnings if approaching the limit.

        Raises ``RuntimeError`` if the daily limit has been reached.
        """
        now = time.time()
        self._prune_timestamps(now)

        count = len(self._request_timestamps)

        # Check if limit reached
        if count >= self._max_requests_per_day:
            self._log.error(
                "groq_rate_limit_exceeded",
                count=count,
                max_per_day=self._max_requests_per_day,
            )
            raise RuntimeError(
                f"Groq daily request limit reached: {count}/{self._max_requests_per_day}. "
                "Skipping AI trading cycle until window resets."
            )

        # Warning levels
        if count >= self._warning_level_3 and self._rate_limit_warning_sent < 3:
            self._log.warning(
                "groq_rate_limit_critical",
                count=count,
                max_per_day=self._max_requests_per_day,
            )
            self._rate_limit_warning_sent = 3
        elif count >= self._warning_level_2 and self._rate_limit_warning_sent < 2:
            self._log.warning(
                "groq_rate_limit_high",
                count=count,
                max_per_day=self._max_requests_per_day,
            )
            self._rate_limit_warning_sent = 2
        elif count >= self._warning_level_1 and self._rate_limit_warning_sent < 1:
            self._log.warning(
                "groq_rate_limit_warning",
                count=count,
                max_per_day=self._max_requests_per_day,
            )
            self._rate_limit_warning_sent = 1

    # ------------------------------------------------------------------
    # Chat completion
    # ------------------------------------------------------------------

    async def chat(
        self,
        system: str | None = None,
        user: str | None = None,
        messages: list[dict[str, str]] | None = None,
        *,
        temperature: float = _DEFAULT_TEMPERATURE,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        model: str | None = None,
    ) -> str:
        """Send a chat completion request to Groq.

        Parameters
        ----------
        system:
            Optional system prompt (prepended as a system message).
        user:
            Optional user message.
        messages:
            Optional full message list.  If provided, ``system`` and
            ``user`` are ignored.
        temperature:
            Sampling temperature (0.0-1.0).  Lower = more deterministic.
        max_tokens:
            Maximum tokens in the response.
        model:
            Override the default model for this request.

        Returns
        -------
        str
            The response text from the assistant.

        Raises
        ------
        RuntimeError
            If no API key is configured.
        groq.APIError
            If the API returns an unrecoverable error.
        """
        if not self._api_key:
            msg = (
                "Groq API key is not configured. "
                "Set the GROQ_API_KEY environment variable or pass api_key."
            )
            self._log.error("groq_api_key_missing")
            raise RuntimeError(msg)

        # Build messages list
        if messages is None:
            msgs: list[dict[str, str]] = []
            if system:
                msgs.append({"role": "system", "content": system})
            if user:
                msgs.append({"role": "user", "content": user})
        else:
            msgs = messages

        if not msgs:
            self._log.warning("groq_empty_messages")
            return ""

        # Ensure the client is initialised
        await self._ensure_client()

        active_model = model or self._model
        last_error: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                # Check rate limit before making the request
                await self._check_rate_limit()

                completion = await self._client.chat.completions.create(
                    model=active_model,
                    messages=msgs,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                self._total_requests += 1
                self._request_timestamps.append(time.time())
                return completion.choices[0].message.content or ""

            except RateLimitError as exc:
                self._total_retries += 1
                self._last_rate_limit = asyncio.get_event_loop().time()
                wait = self._base_backoff * (2 ** (attempt - 1)) + (
                    hash(str(exc)) % 50
                ) / 100.0  # jitter

                self._log.warning(
                    "groq_rate_limited",
                    attempt=attempt,
                    wait_s=round(wait, 2),
                    max_retries=self._max_retries,
                )

                if attempt < self._max_retries:
                    await asyncio.sleep(wait)
                    last_error = exc
                else:
                    raise

            except APIConnectionError as exc:
                self._total_retries += 1
                wait = self._base_backoff * (2 ** (attempt - 1))

                self._log.warning(
                    "groq_connection_error",
                    attempt=attempt,
                    wait_s=round(wait, 2),
                )

                if attempt < self._max_retries:
                    await asyncio.sleep(wait)
                    last_error = exc
                else:
                    raise

            except APIStatusError as exc:
                self._log.error(
                    "groq_api_error",
                    status_code=exc.status_code,
                    response=str(exc.response)[:500],
                )
                raise

        # Should not reach here, but satisfy the return type
        if last_error:
            raise last_error  # type: ignore[misc]
        return ""

    # ------------------------------------------------------------------
    # Structured trading decision
    # ------------------------------------------------------------------

    async def decide_trades(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Request a structured trading decision from the LLM.

        Sends the system and user prompts with configurable temperature for
        deterministic output, expects a JSON response conforming to the
        format defined in the system prompt.

        Parameters
        ----------
        system_prompt:
            System prompt with role definition and output format.
        user_prompt:
            User prompt with market data, context, and decision request.
        temperature:
            Sampling temperature. Falls back to config or 0.0.
        max_tokens:
            Maximum tokens in the response. Falls back to config or 2048.

        Returns
        -------
        dict
            Parsed JSON decision dict with keys: reasoning, action,
            contract, side, quantity, order_type, limit_price, strategy,
            confidence, risk_checks.

        Raises
        ------
        ValueError
            If the LLM response is not valid JSON or missing required keys.
        RuntimeError
            If the API key is missing or rate limit is exceeded.
        """
        effective_temperature = (
            temperature if temperature is not None
            else self._groq_config.get("decide_trades_temperature")
        )
        effective_max_tokens = (
            max_tokens if max_tokens is not None
            else self._groq_config.get("decide_trades_max_tokens")
        )
        raw = await self.chat(
            system=system_prompt,
            user=user_prompt,
            temperature=effective_temperature,
            max_tokens=effective_max_tokens,
        )

        return self._parse_trading_decision(raw)

    def _parse_trading_decision(self, raw: str) -> dict[str, Any]:
        """Parse the LLM response into a structured trading decision dict.

        Attempts to extract JSON from the response (handles markdown code
        fences) and validates required top-level keys.
        """
        text = raw.strip()

        # Strip markdown code fences if present
        if text.startswith("```"):
            # Find the first { or [ to skip fence markers
            first_brace = text.find("{")
            last_brace = text.rfind("}")
            if first_brace != -1 and last_brace != -1:
                text = text[first_brace : last_brace + 1]
            else:
                # Try to find JSON after the fence
                lines = text.split("\n")
                cleaned: list[str] = []
                for line in lines:
                    stripped = line.strip()
                    if stripped.startswith("```"):
                        continue
                    cleaned.append(line)
                text = "\n".join(cleaned).strip()

        try:
            decision: dict[str, Any] = json.loads(text)
        except json.JSONDecodeError as exc:
            self._log.error(
                "groq_invalid_json",
                error=str(exc),
                response_preview=raw[:500],
            )
            # Return a safe HOLD decision on parse failure
            return {
                "reasoning": f"Failed to parse LLM response: {exc}",
                "action": "HOLD",
                "contract": None,
                "side": None,
                "quantity": None,
                "order_type": None,
                "limit_price": None,
                "strategy": None,
                "confidence": 0.0,
                "risk_checks": {},
            }

        # Validate required keys
        required = ["action", "reasoning"]
        for key in required:
            if key not in decision:
                self._log.warning(
                    "groq_decision_missing_key",
                    key=key,
                    decision=str(decision)[:300],
                )
                decision[key] = "HOLD" if key == "action" else "Missing field"

        # Ensure action is one of the expected values
        valid_actions = set(self._groq_config.get("valid_actions"))
        default_action = self._groq_config.get("default_action")
        fallback_action = self._groq_config.get("fallback_action")
        action = decision.get("action", default_action)
        if action not in valid_actions:
            self._log.warning(
                "groq_decision_invalid_action",
                action=action,
            )
            decision["action"] = fallback_action

        return decision

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_client(self) -> None:
        """Client is created eagerly in ``__init__``; this is a no-op."""
        pass

    async def close(self) -> None:
        """Close the underlying HTTP client session.

        Safe to call multiple times.
        """
        try:
            await self._client.close()
        except Exception as exc:
            self._log.warning("groq_client_close_error", error=str(exc))
        self._log.debug("groq_client_closed")
