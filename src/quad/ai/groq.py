"""Async Groq client wrapper for Quad.

Provides a production-ready wrapper around ``groq.AsyncGroq`` with:
- API key authentication (from parameter, env var, or config)
- Configurable model selection with sensible defaults
- Rate-limit awareness and automatic retry jitter
- Sliding-window rate limiter (max requests per day)
- Daily TOKEN-budget throttle (chars/4 estimate) so ``is_available()``
  reports ``False`` once the day's token quota is spent instead of the
  caller burning HTTP 429s
- Structured error handling with structlog logging
- Connection timeout and max-retry configuration

Usage
-----
.. code-block:: python

    client = GroqClient(api_key="...")
    response = await client.chat(
        system="You are a trading assistant.",
        user="Analyze BTCUSDT funding rates and order book...",
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
import re
import time
from collections import deque
from typing import Any

import structlog
from groq import (
    APIConnectionError,
    APIStatusError,
    AsyncGroq,
    RateLimitError,
)

from quad.ai.validator import canonical_direction

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MODEL = "groq/compound-mini"
"""Default model: Groq-native LLM, returns clean parseable structured JSON.

Verified against the configured GROQ_API_KEY (2026-08): the original default
``llama-3.1-8b-instant`` is no longer served (404 model_not_found, fatal).
``groq/compound-mini`` is served and returns action/reasoning JSON directly in
``content``, AND accepts the bot's realistic ~8KB prompt. NOTE: the larger
``groq/compound`` advertises a 131K context_window but rejects the bot's prompt
with 413 request_too_large (empirically ~2.5K-token input is rejected), so it
is NOT usable. ``groq/compound-mini`` handled the full prompt and returned
clean JSON.
"""

_FALLBACK_MODEL = "qwen/qwen3.6-27b"
"""Fallback model if the primary is unavailable or rate-limited.
Deliberately distinct from ``_DEFAULT_MODEL`` so a failure of the primary does
not dead-end the rotation. ``qwen/qwen3.6-27b`` is served on the same key and
accepts the full prompt, but MAY prefix a ``thinking`` block in its output;
``safe_parse_ai_response`` strips non-JSON prose and extracts the final JSON
object, so this is handled. (``openai/gpt-oss-20b``/``-120b`` are not used:
they return output in a separate ``reasoning`` field and empty ``content``,
which would fail JSON parsing; ``groq/compound`` returns 413 on this prompt.)"""

_DEFAULT_MAX_TOKENS_PER_DAY = 100_000
"""Daily token budget for the default model (groq/compound-mini).

The Groq free tier is quota-bound by TOKENS per day.  ``groq/compound-mini``
(served behind ``llama-3.3-70b-versatile`` in 2026-08) reports a hard 429
wall of ``Limit 100000`` tokens/day — NOT the 500K/day budget of the retired
llama-3.1-8b-instant free tier.  The local throttle is deliberately set to
match that real quota so ``is_available()`` trips *before* the API starts
burning HTTP 429s instead of hammering the wall every cycle.  With a
~6-10K-token per-request estimate that allows roughly 10-16 requests/day;
the rotation therefore pauses (``ai_rate_limit_hit_stopping_scan``) for the
rest of the UTC day and resumes after the 24h window slides past the spend."""

_DEFAULT_MAX_TOKENS = 1024
_DEFAULT_TEMPERATURE = 0.3

_FALLBACK_LONG_WAIT_S = 60.0
"""Server-computed ``retry_after`` threshold (seconds) above which the primary
429 is treated as a coarse daily/TPM quota wall.

When the server asks us to wait at least this long, retrying the SAME model
(which would sleep minutes per attempt) is pointless within one rotation
cycle, so ``chat`` routes to the fallback model immediately instead of
sleeping.  Below this threshold (a momentary TPM burst) the normal backoff
retry loop still applies.
"""

_TOKEN_CHARS_PER_TOKEN = 4
"""Chars-per-token heuristic for dependency-light token estimation."""

_RETRY_AFTER_TEXT_RE = re.compile(
    r"try again in\s+([0-9a-zA-Z.]+)", re.IGNORECASE
)
"""Captures the duration token after ``try again in`` in Groq 429 bodies.

The per-minute (TPM) and per-day token quotas both report the
server-computed wait this way; honouring it lets a retry land after the
token bucket refills instead of burning retries on an early resend.

The token is deliberately character-classed (digits, letters, dots) rather
than ``[\\d.]+s`` because the daily-token-quota refusals spell the wait with
minute components, e.g. ``... Please try again in 43m52.608s`` or
``2h3m5s`` — a plain ``([\\d.]+)s`` regex fails on the ``m`` and the retry
would fall back to a tiny exponential backoff against a ~43-minute wall.
"""

_DURATION_PARTS_RE = re.compile(
    r"^\s*"
    r"(?:(?P<hours>\d+)\s*h\s*)?"
    r"(?:(?P<minutes>\d+)\s*m\s*)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)\s*s\s*)?"
    r"$",
    re.IGNORECASE,
)
"""Parses a compact duration string into its hour/minute/second parts.

Handles seconds (``21.84s``), minutes+seconds (``43m52.608s``), and
hours+minutes+seconds (``2h3m5s``) with optional inter-unit whitespace.
"""


def _parse_duration(raw: str | None) -> float | None:
    """Parse a compact duration like ``43m52.608s`` to total seconds.

    Accepts seconds (``21.84s``), minutes+seconds (``43m52.608s``), and
    hours+minutes+seconds (``2h3m5s``).  Returns ``None`` when no valid
    duration parts are present so the caller can fall back to its own
    backoff.
    """
    if not raw:
        return None
    text = raw.strip().rstrip(".")
    if not text:
        return None
    match = _DURATION_PARTS_RE.match(text)
    if not match or not any(match.groupdict().values()):
        return None
    total = 0.0
    if match.group("hours"):
        total += int(match.group("hours")) * 3600
    if match.group("minutes"):
        total += int(match.group("minutes")) * 60
    if match.group("seconds"):
        total += float(match.group("seconds"))
    return total


# ---------------------------------------------------------------------------
# Safe JSON parsing for LLM responses
# ---------------------------------------------------------------------------


def safe_parse_ai_response(raw: str) -> str:
    """Clean AI response text to extract valid JSON string.

    Handles:
    - Markdown code fences (`` ```json `` / `` ``` ``)
    - Standalone ``"reasoning"`` or ``"thinking"`` fields that appear as a
      separate JSON object before the actual trading decision
    - Multiple consecutive JSON objects (the reasoning block + the decision)
    - Partial / incomplete JSON wrapping
    - Text before / after the JSON payload

    The function finds ALL complete JSON objects in the response by tracking
    brace depth (correctly handling strings with ``{``/``}`` and escape
    sequences), and returns the **last** one — reasoning blocks typically
    come first, and the actual trading decision is the last valid JSON object.

    Returns a clean JSON string ready for ``json.loads``.
    """
    # Strip code fences
    cleaned = re.sub(r"```json|```", "", raw).strip()
    if not cleaned:
        return "{}"

    # Find all complete JSON objects by tracking brace depth.
    # Properly handles strings with {, }, or escape sequences inside them.
    json_objects: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escape = False

    for i, ch in enumerate(cleaned):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                json_objects.append(cleaned[start : i + 1])
                start = -1

    if json_objects:
        # Return the LAST complete JSON object (the actual trading decision),
        # because models often prepend a reasoning/thinking block as a
        # separate JSON object before the decision payload.
        return json_objects[-1]

    # Fallback: try to find a JSON object anywhere in the remaining text
    if not cleaned.startswith("{"):
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            return match.group()

    return cleaned


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
        Defaults to ``groq/compound-mini``.
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
        # Fallback model for graceful degradation when the primary is
        # rate-limited or token-budget exhausted.  Configurable via
        # ``ai.groq.fallback_model``; falls back to the module constant.
        self._fallback_model = self._groq_config.get(
            "fallback_model", _FALLBACK_MODEL
        )
        self._timeout = timeout or self._groq_config.get("timeout_seconds")
        self._max_retries = max_retries or self._groq_config.get("max_retries")

        # Rate limiter / backoff configuration
        rate_limiter_cfg = self._groq_config["rate_limiter"]
        # Fall back to a sensible default when the key is absent so that
        # comparisons in is_available / _check_rate_limit never see None.
        self._max_requests_per_day = (
            max_requests_per_day or rate_limiter_cfg.get("max_requests_per_day") or 1000
        )
        self._rate_limit_window_s = rate_limiter_cfg.get("window_seconds")
        self._warning_level_1 = rate_limiter_cfg.get("warning_level_1")
        self._warning_level_2 = rate_limiter_cfg.get("warning_level_2")
        self._warning_level_3 = rate_limiter_cfg.get("warning_level_3")
        self._base_backoff = self._groq_config.get("base_backoff_seconds")

        # Daily token-budget throttle.  The Groq free tier is token-quota
        # bound (500K tokens/day for the 8b model); requests-per-day alone
        # under-counts the real constraint.  Fall back to sane defaults when
        # the nested config key is absent (tests / minimal configs).
        token_budget_cfg = self._groq_config.get("token_budget", {}) or {}
        self._token_budget_enabled = bool(token_budget_cfg.get("enabled", True))
        self._max_tokens_per_day = int(
            token_budget_cfg.get("max_tokens_per_day") or _DEFAULT_MAX_TOKENS_PER_DAY
        )
        self._token_window_s = float(token_budget_cfg.get("window_seconds") or 86400)
        self._token_warning_level_1 = int(
            token_budget_cfg.get("warning_level_1") or 80_000
        )
        self._token_warning_level_2 = int(
            token_budget_cfg.get("warning_level_2") or 90_000
        )
        self._token_warning_level_3 = int(
            token_budget_cfg.get("warning_level_3") or 95_000
        )

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

        # Daily token budget: rolling deque of (timestamp, tokens_used).
        self._token_usages: deque[tuple[float, int]] = deque()
        self._total_tokens_estimated: int = 0
        self._token_warning_sent: int = 0  # tracks highest token warning level

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
            "token_budget_enabled": self._token_budget_enabled,
            "tokens_used_in_window": self.tokens_used_in_window(now),
            "total_tokens_estimated": self._total_tokens_estimated,
            "max_tokens_per_day": self._max_tokens_per_day,
            "available": self.is_available(now),
        }

    # ------------------------------------------------------------------
    # Availability check
    # ------------------------------------------------------------------

    def is_available(self, now: float | None = None) -> bool:
        """Check if the client is available for trading decisions.

        Availability is determined solely by the presence of an API key.
        The daily request and token-budget caps were removed: local quota
        arithmetic no longer gates AI.  Real quota exhaustion is surfaced by
        the Groq API's own 429s and handled by the retry/backoff/fallback
        path instead of a local ``is_available()`` veto.

        Parameters
        ----------
        now:
            Kept for signature compatibility; unused.
        """
        return bool(self._api_key)

    # ------------------------------------------------------------------
    # Token budget (daily token-quota throttle)
    # ------------------------------------------------------------------

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Estimate the token count of ``text`` with a chars/4 heuristic.

        Deliberately dependency-light: no tokenizer is pulled in.  The
        daily budget is a coarse safety valve, so a modest
        over/under-estimate is acceptable; we err toward over-estimating
        (see ``chat`` where the output cap is added) so the throttle
        under-consumes rather than overspending the real quota.
        """
        if not text:
            return 0
        return max(1, len(text) // _TOKEN_CHARS_PER_TOKEN)

    def tokens_used_in_window(self, now: float | None = None) -> int:
        """Return estimated tokens consumed within the rolling window."""
        self._prune_token_usages(now)
        return sum(t for _, t in self._token_usages)

    def _prune_token_usages(self, now: float | None = None) -> None:
        """Drop token-usage entries older than the rolling window."""
        if now is None:
            now = time.time()
        cutoff = now - self._token_window_s
        while self._token_usages and self._token_usages[0][0] < cutoff:
            self._token_usages.popleft()

    def _record_token_usage(
        self,
        tokens: int,
        now: float | None = None,
    ) -> None:
        """Record an estimated token spend at ``now`` (default: real time).

        Entries older than ``window_seconds`` are pruned on the next
        read, which gives the daily reset behaviour (usage rolls off once
        the window slides past it).
        """
        if tokens <= 0 or not self._token_budget_enabled:
            return
        if now is None:
            now = time.time()
        self._prune_token_usages(now)
        self._token_usages.append((now, tokens))
        self._total_tokens_estimated += tokens

    async def _check_token_budget(
        self,
        estimate: int,
        now: float | None = None,
    ) -> None:
        """Daily token-budget throttle.

        No-op: the daily token-budget cap was removed.  Kept for call-site
        compatibility; real quota limits surface as API 429s and are handled
        by the retry/backoff/fallback path rather than a local veto.

        Parameters
        ----------
        estimate:
            Kept for signature compatibility; unused.
        now:
            Kept for signature compatibility; unused.

        Raises
        ------
        RuntimeError
            Previously raised when ``used + estimate`` would exceed
            ``max_tokens_per_day``; the daily token-budget cap was removed,
            so this no longer raises.
        """
        return

    # ------------------------------------------------------------------
    # Rate limiter
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_retry_after(exc: RateLimitError) -> float | None:
        """Extract the server-recommended retry delay from a Groq 429.

        Groq 429 bodies carry ``... Please try again in 21.84s.`` for the
        per-minute (TPM) quota and ``... try again in 43m52.608s`` /
        ``2h3m5s`` for the daily-token quota.  ``APIStatusError`` surfaces
        the parsed JSON body as ``exc.body``; some proxies set the standard
        ``Retry-After`` header instead, which is honoured as a fallback.

        Returns the wait in seconds, or ``None`` when no usable value is
        present so the caller can fall back to its exponential backoff.
        """
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            message = str(body.get("message", ""))
            if not message and isinstance(body.get("error"), dict):
                message = str(body["error"].get("message", ""))
        else:
            message = str(body or "")
        match = _RETRY_AFTER_TEXT_RE.search(message)
        if match and match.group(1):
            parsed = _parse_duration(match.group(1))
            if parsed is not None:
                return parsed

        headers = getattr(getattr(exc, "response", None), "headers", None) or {}
        raw = headers.get("retry-after") or headers.get("Retry-After")
        if raw:
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
        return None

    def _prune_timestamps(self, now: float | None = None) -> None:
        """Remove timestamps outside the sliding window."""
        if now is None:
            now = time.time()
        cutoff = now - self._rate_limit_window_s
        while self._request_timestamps and self._request_timestamps[0] < cutoff:
            self._request_timestamps.popleft()

    async def _check_rate_limit(self) -> None:
        """Request-rate throttle.

        No-op: the daily request cap was removed.  Kept for call-site
        compatibility; real quota limits surface as API 429s and are handled
        by the retry/backoff/fallback path rather than a local veto.
        """
        return

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
        json_mode: bool = False,
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
        json_mode:
            If True, request structured JSON output from the model via
            ``response_format={"type": "json_object"}``.  Use for
            structured decisions that must parse as JSON.

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

        # Estimate the token cost of this request BEFORE sending so the
        # daily token budget can refuse before the API burns a 429.  Input is
        # the char/4 estimate of every message; output is the (conservative)
        # max-token cap, which over-counts on purpose so the throttle never
        # overspends the real quota.
        input_chars = sum(len(str(m.get("content", ""))) for m in msgs)
        estimated_total_tokens = (input_chars // _TOKEN_CHARS_PER_TOKEN) + int(
            max_tokens or _DEFAULT_MAX_TOKENS
        )

        return await self._chat(
            active_model=active_model,
            msgs=msgs,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
            estimated_total_tokens=estimated_total_tokens,
            allow_fallback=True,
        )

    async def _chat(
        self,
        *,
        active_model: str,
        msgs: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        json_mode: bool,
        estimated_total_tokens: int,
        allow_fallback: bool,
    ) -> str:
        """Run the request/retry loop for one model, with fallback support.

        ``allow_fallback`` gates the graceful-degradation path: when the
        active model is exhausted (daily-token 429 or local token-budget
        refusal) and a different fallback model is configured, the request is
        retried ONCE with the fallback instead of letting the error propagate
        (which would otherwise log ``ai_scan_failed`` and force a HOLD).
        """
        last_error: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                # Local rate / token-budget pre-checks guard the PRIMARY's
                # quota.  A fallback rescue (``allow_fallback=False``) must NOT
                # be blocked by them: the fallback model has its own server-side
                # quota, which the real 429 + backoff path below enforces.  If
                # the primary was refused solely because its local budget is
                # spent (shared deque), the fallback would otherwise re-raise
                # the same refusal and the rescue would never fire -- producing
                # ai_scan_failed instead of a decision.
                if allow_fallback:
                    await self._check_rate_limit()
                    await self._check_token_budget(estimated_total_tokens)

                create_kwargs: dict[str, Any] = {
                    "model": active_model,
                    "messages": msgs,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if json_mode:
                    create_kwargs["response_format"] = {"type": "json_object"}
                completion = await self._client.chat.completions.create(**create_kwargs)
                self._total_requests += 1
                self._request_timestamps.append(time.time())
                self._record_token_usage(estimated_total_tokens)
                return completion.choices[0].message.content or ""

            except RateLimitError as exc:
                self._total_retries += 1
                self._last_rate_limit = asyncio.get_event_loop().time()
                # Groq 429s carry the server-computed wait (``retry_after``),
                # e.g. a TPM refusal with "Please try again in 21.84s."  Prefer
                # it over the exponential backoff so the retry lands after the
                # token bucket refills; the local estimate (chars/4) is far
                # below the true TPM quota, so a fixed backoff would resend too
                # early and burn every retry.
                retry_after = self._extract_retry_after(exc)
                base_wait = self._base_backoff * (2 ** (attempt - 1))
                wait = (
                    max(retry_after, base_wait)
                    if retry_after is not None
                    else base_wait
                )
                wait += (hash(str(exc)) % 50) / 100.0  # jitter

                self._log.warning(
                    "groq_rate_limited",
                    model=active_model,
                    fallback=(active_model == self._fallback_model),
                    attempt=attempt,
                    wait_s=round(wait, 2),
                    retry_after_s=retry_after,
                    max_retries=self._max_retries,
                )

                # Long server-computed wait (e.g. the daily-token-quota 429
                # "try again in 43m52.608s"): retrying the SAME primary after
                # minutes-per-attempt is pointless within this cycle, so route
                # to the fallback model immediately instead of sleeping.
                if (
                    retry_after is not None
                    and retry_after >= _FALLBACK_LONG_WAIT_S
                    and self._try_fallback(active_model, allow_fallback)
                ):
                    self._log.warning(
                        "groq_primary_rate_limited_trying_fallback",
                        model=active_model,
                        fallback_model=self._fallback_model,
                        retry_after_s=round(retry_after, 2),
                    )
                    return await self._chat(
                        active_model=self._fallback_model,
                        msgs=msgs,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        json_mode=json_mode,
                        estimated_total_tokens=estimated_total_tokens,
                        allow_fallback=False,
                    )

                if attempt < self._max_retries:
                    await asyncio.sleep(wait)
                    last_error = exc
                elif self._try_fallback(active_model, allow_fallback):
                    return await self._chat(
                        active_model=self._fallback_model,
                        msgs=msgs,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        json_mode=json_mode,
                        estimated_total_tokens=estimated_total_tokens,
                        allow_fallback=False,
                    )
                else:
                    raise

            except RuntimeError as exc:
                # Local request / token-budget refusal (is_available-trip).
                # Route to the fallback model once before propagating; an
                # exhausted fallback still raises so the orchestrator breaks.
                if self._is_budget_refusal(exc) and self._try_fallback(
                    active_model, allow_fallback
                ):
                    self._log.warning(
                        "groq_primary_budget_exhausted_trying_fallback",
                        model=active_model,
                        fallback_model=self._fallback_model,
                        reason=str(exc),
                    )
                    return await self._chat(
                        active_model=self._fallback_model,
                        msgs=msgs,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        json_mode=json_mode,
                        estimated_total_tokens=estimated_total_tokens,
                        allow_fallback=False,
                    )
                raise

            except APIConnectionError as exc:
                self._total_retries += 1
                wait = self._base_backoff * (2 ** (attempt - 1))

                self._log.warning(
                    "groq_connection_error",
                    model=active_model,
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
                    model=active_model,
                    status_code=exc.status_code,
                    response=str(exc.response)[:500],
                )
                raise

        # Should not reach here, but satisfy the return type
        if last_error:
            raise last_error
        return ""

    @staticmethod
    def _is_budget_refusal(exc: RuntimeError) -> bool:
        """True when ``exc`` is a local request/token-budget refusal."""
        text = str(exc).lower()
        return "rate limit" in text or "token budget" in text

    def _try_fallback(self, active_model: str, allow_fallback: bool) -> bool:
        """Whether a fallback retry should be attempted for ``active_model``."""
        return (
            allow_fallback
            and bool(self._fallback_model)
            and active_model != self._fallback_model
        )

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
            side, contract, quantity.  Legacy keys (order_type,
            limit_price, strategy, confidence, risk_checks) are
            tolerated when present and defaulted when absent.  On parse
            failure returns a safe HOLD dict with an explanatory
            reasoning field.

        Raises
        ------
        RuntimeError
            If the API key is missing or rate limit is exceeded.
        """
        effective_temperature = (
            temperature
            if temperature is not None
            else self._groq_config.get("decide_trades_temperature")
        )
        effective_max_tokens = (
            max_tokens
            if max_tokens is not None
            else self._groq_config.get("decide_trades_max_tokens")
        )
        raw = await self.chat(
            system=system_prompt,
            user=user_prompt,
            temperature=effective_temperature,
            max_tokens=effective_max_tokens,
            json_mode=True,
        )

        return self._parse_trading_decision(raw)

    def _parse_trading_decision(self, raw: str) -> dict[str, Any]:
        """Parse the LLM response into a structured trading decision dict.

        Uses ``safe_parse_ai_response`` to clean the raw text (handles
        markdown code fences and partial / incomplete JSON wrapping), then
        validates required top-level keys.
        """
        try:
            text = safe_parse_ai_response(raw)
            decision: dict[str, Any] = json.loads(text)
            if not isinstance(decision, dict):
                self._log.warning(
                    "groq_decision_not_dict",
                    type=type(decision).__name__,
                    preview=str(decision)[:200],
                )
                return {
                    "reasoning": f"LLM returned a {type(decision).__name__}, expected a JSON object",
                    "action": "HOLD",
                    "direction": "NEUTRAL",
                    "confidence": 0.0,
                    "indicators": {},
                }
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
                "direction": "NEUTRAL",
                "contract": None,
                "side": None,
                "quantity": None,
                "order_type": "MARKET",
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
        # NOTE: explicit fallback defaults protect against missing config keys;
        # Pydantic normally provides them but the raw dict may not always have them.
        valid_actions = set(
            self._groq_config.get("valid_actions", ["ENTER", "EXIT", "HOLD"])
        )
        default_action = self._groq_config.get("default_action", "HOLD")
        fallback_action = self._groq_config.get("fallback_action", "HOLD")
        action = decision.get("action", default_action)
        if action not in valid_actions:
            self._log.warning(
                "groq_decision_invalid_action",
                action=action,
            )
            decision["action"] = fallback_action

        # The simplified prompt no longer asks for order_type / limit_price /
        # strategy / risk_checks.  Tolerate decisions that omit them by
        # defaulting gracefully so downstream consumers never KeyError.
        # All orders are MARKET (no limit orders), so the default order_type
        # is MARKET rather than None.
        decision.setdefault("order_type", "MARKET")
        decision.setdefault("limit_price", None)
        decision.setdefault("strategy", None)
        decision.setdefault("risk_checks", {})
        decision.setdefault("confidence", 0.0)

        # Canonicalize the direction field (Phase 1 inversion guard).  The
        # model states a DIRECTION; the bot derives the order side from it.
        # Tolerate the legacy raw ``side`` field as a backward-compat source
        # of direction when the model omits the new field.
        if not decision.get("direction") and decision.get("side"):
            decision["direction"] = decision.get("side")
        decision["direction"] = canonical_direction(decision.get("direction"))

        return decision

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_client(self) -> None:
        """Client is created eagerly in ``__init__``; this is a no-op."""

    async def close(self) -> None:
        """Close the underlying HTTP client session.

        Safe to call multiple times.
        """
        try:
            await self._client.close()
        except Exception as exc:
            self._log.warning("groq_client_close_error", error=str(exc))
        self._log.debug("groq_client_closed")
