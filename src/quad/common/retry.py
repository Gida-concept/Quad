"""Shared retry and backoff helpers.

Four places in the codebase hand-rolled the same "wait a bit longer after each
failure" arithmetic -- the Groq client (twice), the Bybit REST client, and the
order-submission gateway -- and two of them (Bybit, gateway) hand-rolled the
same bounded retry loop as well.  Those copies drift: one caps at 30 s, one at
60 s, one jitters with ``hash(exc)`` and one does not.

This module owns the arithmetic and the loop; callers keep their own *policy*
(what counts as retryable, what to log, whether to fall back to another model
or key), which is the part that legitimately differs.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

__all__ = ["exponential_backoff", "retry_async"]

#: Result type of a retried operation.
T = TypeVar("T")


def exponential_backoff(
    attempt: int,
    base: float,
    *,
    multiplier: float = 2.0,
    cap: float | None = None,
    jitter: float = 0.0,
) -> float:
    """Return the delay before retry *attempt* (1-based).

    The delay grows geometrically from *base*: ``base * multiplier**(attempt-1)``
    -- so attempt 1 waits ``base``, attempt 2 waits ``base * multiplier``, and so
    on.  *cap* bounds it (pass ``None`` for no bound), and *jitter* adds a random
    component in ``[0, jitter)`` so concurrent callers that failed together do
    not retry in lockstep.

    Args:
        attempt: 1-based attempt number.  ``1`` means "the wait before the
            first retry", i.e. no failures beyond the first have happened yet.
        base: The first delay, in seconds.
        multiplier: Growth factor per attempt.
        cap: Maximum delay in seconds, or ``None`` for unbounded growth.
        jitter: Width of the uniform random addition, in seconds.
    """
    if attempt < 1:
        raise ValueError(f"attempt must be >= 1, got {attempt}")
    delay = base * (multiplier ** (attempt - 1))
    if cap is not None:
        delay = min(delay, cap)
    if jitter:
        delay += random.uniform(0.0, jitter)
    return delay


async def retry_async(
    operation: Callable[[int], Awaitable[T]],
    *,
    attempts: int,
    is_retryable: Callable[[Exception], bool],
    delay_for: Callable[[int, Exception], float],
    before_attempt: Callable[[int], Awaitable[None]] | None = None,
    on_retry: Callable[[int, Exception, float], None] | None = None,
) -> T:
    """Await *operation* until it succeeds, fails non-retryably, or runs out.

    Args:
        operation: Coroutine function taking the 1-based attempt number and
            returning the result.  It should raise on failure.
        attempts: Total number of attempts, including the first.  Must be >= 1.
        is_retryable: Predicate over the exception raised by *operation*.
            Returning ``False`` re-raises immediately, without further retries.
        delay_for: ``(attempt, exception) -> seconds`` to wait before the next
            attempt.  Called only when a retry will actually happen.
        before_attempt: Optional coroutine awaited before each attempt -- for
            throttles or budget checks.  Not called if the previous attempt
            exhausted the budget.
        on_retry: Optional callback invoked as ``(attempt, exception, delay)``
            just before sleeping, for logging.

    Returns:
        Whatever *operation* returned.

    Raises:
        Exception: The last exception raised by *operation*, re-raised once the
            attempts are exhausted, so exhaustion is never silently swallowed.
    """
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts}")

    for attempt in range(1, attempts + 1):
        if before_attempt is not None:
            await before_attempt(attempt)
        try:
            return await operation(attempt)
        except Exception as exc:
            if not is_retryable(exc):
                raise
            if attempt >= attempts:
                raise
            delay = delay_for(attempt, exc)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            if delay > 0:
                await asyncio.sleep(delay)
    # Unreachable: the loop either returns or raises on every path.  The guard
    # keeps the return type honest if that ever stops being true.
    raise RuntimeError("retry_async exhausted without returning or raising")
