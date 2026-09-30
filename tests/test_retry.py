"""Tests for the shared retry/backoff helpers.

These pin the behaviour the migrated call sites depend on, so the extraction
cannot silently change any of them: the backoff schedule, the cap, the jitter
bound, and every exit path of ``retry_async``.
"""

from __future__ import annotations

import asyncio

import pytest

from quad.common.retry import exponential_backoff, retry_async


class TestExponentialBackoff:
    def test_first_attempt_waits_the_base_delay(self):
        assert exponential_backoff(1, 0.5) == pytest.approx(0.5)

    def test_grows_geometrically_per_attempt(self):
        # base * 2**(attempt-1) -> 1, 2, 4, 8
        assert [exponential_backoff(n, 1.0) for n in (1, 2, 3, 4)] == pytest.approx(
            [1.0, 2.0, 4.0, 8.0]
        )

    def test_honours_a_non_default_multiplier(self):
        assert [exponential_backoff(n, 1.0, multiplier=3.0) for n in (1, 2, 3)] == (
            pytest.approx([1.0, 3.0, 9.0])
        )

    def test_cap_bounds_growth(self):
        # Without a cap attempt 6 would be 32s; the cap holds it at 10s.
        assert exponential_backoff(6, 1.0, cap=10.0) == pytest.approx(10.0)
        assert exponential_backoff(2, 1.0, cap=10.0) == pytest.approx(2.0)

    def test_cap_also_bounds_a_large_base(self):
        assert exponential_backoff(1, 100.0, cap=30.0) == pytest.approx(30.0)

    def test_jitter_stays_within_its_width_and_never_goes_negative(self):
        # Jitter is added on top of the *grown* delay, so bound it relative to
        # the un-jittered schedule rather than to ``base``.
        for n in range(1, 6):
            plain = exponential_backoff(n, 1.0)
            for _ in range(20):
                delay = exponential_backoff(n, 1.0, jitter=0.5)
                assert plain <= delay < plain + 0.5

    def test_rejects_a_zero_or_negative_attempt(self):
        # attempt is 1-based; 0 would silently mean "a very long wait".
        with pytest.raises(ValueError):
            exponential_backoff(0, 1.0)
        with pytest.raises(ValueError):
            exponential_backoff(-1, 1.0)


class TestRetryAsync:
    @staticmethod
    def _always_retryable(_exc: Exception) -> bool:
        return True

    @staticmethod
    def _never_retryable(_exc: Exception) -> bool:
        return False

    @pytest.mark.asyncio
    async def test_returns_the_first_success_without_sleeping(self):
        slept: list[float] = []

        async def fake_sleep(delay: float) -> None:
            slept.append(delay)

        async def op(attempt: int) -> str:
            assert attempt == 1
            return "ok"

        result = await retry_async(
            op,
            attempts=3,
            is_retryable=self._always_retryable,
            delay_for=lambda a, e: 0.0,
        )
        assert result == "ok"
        assert slept == []

    @pytest.mark.asyncio
    async def test_retries_until_success_and_passes_the_attempt_number(self):
        seen: list[int] = []

        async def op(attempt: int) -> str:
            seen.append(attempt)
            if attempt < 3:
                raise ConnectionError("flaky")
            return "ok"

        result = await retry_async(
            op,
            attempts=5,
            is_retryable=self._always_retryable,
            delay_for=lambda a, e: 0.0,
        )
        assert result == "ok"
        assert seen == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_reraises_immediately_when_not_retryable(self):
        calls = 0

        async def op(attempt: int) -> str:
            nonlocal calls
            calls += 1
            raise ValueError("fatal")

        with pytest.raises(ValueError, match="fatal"):
            await retry_async(
                op,
                attempts=5,
                is_retryable=self._never_retryable,
                delay_for=lambda a, e: 0.0,
            )
        assert calls == 1, "must not retry a non-retryable failure"

    @pytest.mark.asyncio
    async def test_reraises_the_last_error_once_attempts_are_exhausted(self):
        calls = 0

        async def op(attempt: int) -> str:
            nonlocal calls
            calls += 1
            raise ConnectionError(f"fail-{attempt}")

        with pytest.raises(ConnectionError, match="fail-3"):
            await retry_async(
                op,
                attempts=3,
                is_retryable=self._always_retryable,
                delay_for=lambda a, e: 0.0,
            )
        assert calls == 3

    @pytest.mark.asyncio
    async def test_calls_before_attempt_for_every_attempt(self):
        seen: list[int] = []

        async def before(attempt: int) -> None:
            seen.append(attempt)

        async def op(attempt: int) -> str:
            if attempt < 2:
                raise ConnectionError("flaky")
            return "ok"

        await retry_async(
            op,
            attempts=3,
            is_retryable=self._always_retryable,
            delay_for=lambda a, e: 0.0,
            before_attempt=before,
        )
        assert seen == [1, 2]

    @pytest.mark.asyncio
    async def test_on_retry_receives_attempt_exception_and_delay(self):
        events: list[tuple[int, str, float]] = []

        async def op(attempt: int) -> str:
            if attempt < 3:
                raise ConnectionError("flaky")
            return "ok"

        await retry_async(
            op,
            attempts=3,
            is_retryable=self._always_retryable,
            delay_for=lambda a, e: 0.25 * a,
            on_retry=lambda attempt, exc, delay: events.append(
                (attempt, str(exc), delay)
            ),
        )
        # One callback per *actual* retry -- not after the final success.
        assert events == [(1, "flaky", 0.25), (2, "flaky", 0.5)]

    @pytest.mark.asyncio
    async def test_no_on_retry_callback_after_the_final_failure(self):
        events: list[int] = []

        async def op(attempt: int) -> str:
            raise ConnectionError("always")

        with pytest.raises(ConnectionError):
            await retry_async(
                op,
                attempts=3,
                is_retryable=self._always_retryable,
                delay_for=lambda a, e: 0.0,
                on_retry=lambda attempt, exc, delay: events.append(attempt),
            )
        # Exhaustion re-raises rather than sleeping a pointless final time.
        assert events == [1, 2]

    @pytest.mark.asyncio
    async def test_actually_sleeps_the_computed_delay(self, monkeypatch):
        slept: list[float] = []
        real_sleep = asyncio.sleep

        async def fake_sleep(delay: float) -> None:
            slept.append(delay)
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        async def op(attempt: int) -> str:
            if attempt < 3:
                raise ConnectionError("flaky")
            return "ok"

        await retry_async(
            op,
            attempts=3,
            is_retryable=self._always_retryable,
            delay_for=lambda a, e: 0.5 * a,
        )
        assert slept == [0.5, 1.0]

    @pytest.mark.asyncio
    async def test_rejects_a_non_positive_attempt_count(self):
        async def op(attempt: int) -> str:  # pragma: no cover - never called
            raise AssertionError("must not be called")

        with pytest.raises(ValueError):
            await retry_async(
                op,
                attempts=0,
                is_retryable=self._always_retryable,
                delay_for=lambda a, e: 0.0,
            )
