r"""Tests for Groq 429 retry-after parsing.

Covers the daily-token-quota duration forms the previous ``([\d.]+)s`` regex
failed to honour (e.g. ``43m52.608s`` / ``2h3m5s``), which caused the retry
loop to fall back to a tiny exponential backoff against a long server wait.
"""

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quad.ai.groq import _parse_duration  # noqa: E402
from quad.ai.groq import GroqClient  # noqa: E402

_extract_retry_after = GroqClient._extract_retry_after


class _FakeRateLimitError:
    """Duck-typed stand-in exposing just the attributes the parser reads."""

    def __init__(self, body=None, retry_after_header=None):
        self.body = body
        self.response = (
            type("R", (), {"headers": {"Retry-After": retry_after_header}})()
            if retry_after_header is not None
            else None
        )


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("21.84s", 21.84),
        ("43m52.608s", 2632.608),
        ("43m52s", 2632.0),
        ("2h3m5s", 7385.0),
        ("2h", 7200.0),
        ("11m19.104s", 679.104),
        ("1h30m", 5400.0),
        ("0.5s", 0.5),
    ],
)
def test_parse_duration_accepts_compact_forms(raw, expected):
    assert _parse_duration(raw) == pytest.approx(expected)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "bogus",
        "12:34:56",
        None,
    ],
)
def test_parse_duration_rejects_non_durations(raw):
    assert _parse_duration(raw) is None


def test_parse_duration_handles_trailing_period():
    # Groq messages end the wait with a period: "try again in 43m52.608s."
    assert _parse_duration("43m52.608s.") == pytest.approx(2632.608)


def test_extract_retry_after_seconds():
    exc = _FakeRateLimitError(
        body={"message": "Rate limit reached. Please try again in 21.84s."}
    )
    assert _extract_retry_after(exc) == pytest.approx(21.84)


def test_extract_retry_after_minutes_seconds():
    # The daily-token quota refusal — the case the old regex missed.
    exc = _FakeRateLimitError(
        body={
            "message": (
                "Rate limit reached for model `llama-3.3-70b-versatile` ... "
                "on tokens per day (TPD): Limit 100000 ... "
                "Please try again in 43m52.608s."
            )
        }
    )
    result = _extract_retry_after(exc)
    assert result is not None
    assert result == pytest.approx(2632.608)


def test_extract_retry_after_nested_error_dict():
    # Some responses wrap the message under body["error"]["message"].
    exc = _FakeRateLimitError(
        body={
            "error": {
                "message": "Rate limit reached. Please try again in 11m19.104s."
            }
        }
    )
    assert _extract_retry_after(exc) == pytest.approx(679.104)


def test_extract_retry_after_no_body_returns_none():
    assert _extract_retry_after(_FakeRateLimitError(body=None)) is None


def test_extract_retry_after_falls_back_to_header():
    exc = _FakeRateLimitError(body=None, retry_after_header="90")
    assert _extract_retry_after(exc) == pytest.approx(90)


# ---------------------------------------------------------------------------
# Daily token-budget throttle: is_available() trips at the budget and the
# rolling window slides past it to resume (acceptance for Phase 2).
# ---------------------------------------------------------------------------

from quad.ai.groq import GroqClient  # noqa: E402
from unittest import mock  # noqa: E402


def _make_client(**token_budget_overrides):
    # groq 0.4.1 eagerly builds an httpx AsyncClient that is incompatible
    # with the installed httpx (dropped `proxies` kwarg).  Patch AsyncGroq so
    # the tests exercise only the rate-limit/token-budget logic.
    with mock.patch("quad.ai.groq.AsyncGroq"):
        cfg = {
            "ai": {
                "model": "groq/compound-mini",
                "groq": {
                    "timeout_seconds": 30.0,
                    "max_retries": 3,
                    "base_backoff_seconds": 1.0,
                    "rate_limiter": {
                        "max_requests_per_day": 1000,
                        "window_seconds": 86400,
                        "warning_level_1": 800,
                        "warning_level_2": 900,
                        "warning_level_3": 950,
                    },
                    "token_budget": {
                        "enabled": True,
                        "max_tokens_per_day": 100_000,
                        "window_seconds": 86400,
                        "warning_level_1": 80_000,
                        "warning_level_2": 90_000,
                        "warning_level_3": 95_000,
                    },
                },
            }
        }
        cfg["ai"]["groq"]["token_budget"].update(token_budget_overrides)
        return GroqClient(api_key="test-key", config=cfg)


def test_is_available_key_only_regardless_of_usage():
    c = _make_client()
    now = 1_700_000_000.0
    assert c.is_available(now) is True
    c._record_token_usage(60_000, now=now - 100)
    assert c.is_available(now) is True
    c._record_token_usage(45_000, now=now - 50)  # 105_000 >= 100_000 budget
    # Caps removed: availability is key-only and never trips on usage.
    assert c.is_available(now) is True


def test_is_available_false_only_without_api_key():
    c = _make_client()
    c._api_key = ""
    assert c.is_available() is False


def test_check_token_budget_noop_after_caps_removed():
    import asyncio

    c = _make_client()
    now = 1_700_000_000.0
    c._record_token_usage(90_000, now=now)
    # Cap removed: exceeds use but must not raise.
    asyncio.run(c._check_token_budget(20_000, now=now))


def test_availability_ignores_token_usage_window():
    c = _make_client()
    t0 = 1_700_000_000.0
    c._record_token_usage(100_000, now=t0)  # would have exhausted the budget
    assert c.tokens_used_in_window(t0) == 100_000
    # Cap removed: availability stays True regardless of usage/window.
    assert c.is_available(t0) is True
    # One full day + 1s later the spend rolls out of the 86400s window.
    later = t0 + 86_400 + 1
    assert c.tokens_used_in_window(later) == 0
    assert c.is_available(later) is True


# ---------------------------------------------------------------------------
# Fallback model: a long-wait primary 429 routes to the fallback model once
# (acceptance for Phase 3).
# ---------------------------------------------------------------------------

from groq import RateLimitError  # noqa: E402


class _FakeRL(RateLimitError):
    """RateLimitError whose ``__str__`` is safe for the jitter hash."""

    def __str__(self):  # pragma: no cover - trivial
        return "fake-rate-limit"


def _fake_rl(try_again: str) -> _FakeRL:
    e = _FakeRL.__new__(_FakeRL)
    e.body = {"message": f"Rate limit reached. {try_again}"}
    e.response = None
    return e


def _success_response(content: str) -> object:
    return type(
        "Resp",
        (),
        {
            "choices": [
                type(
                    "Choice",
                    (),
                    {"message": type("Msg", (), {"content": content})()},
                )()
            ]
        },
    )()


def test_primary_long_wait_429_falls_back_to_model():
    import asyncio

    c = _make_client()
    c._fallback_model = "qwen/qwen3.6-27b"  # distinct from primary
    called = []

    async def fake_create(**kwargs):
        called.append(kwargs["model"])
        if kwargs["model"] == c._model:  # primary -> daily-quota 429
            raise _fake_rl("Please try again in 43m52.608s.")
        return _success_response('{"action":"ENTER"}')

    c._client.chat.completions.create = fake_create

    out = asyncio.run(
        c.chat(system="s", user="u", temperature=0.3, max_tokens=64)
    )
    assert out == '{"action":"ENTER"}'
    # primary attempted first, then the request completed on the fallback
    assert called[0] == c._model
    assert called[-1] == "qwen/qwen3.6-27b"


def test_fallback_not_used_when_primary_succeeds():
    import asyncio

    c = _make_client()
    c._fallback_model = "qwen/qwen3.6-27b"
    called = []

    async def fake_create(**kwargs):
        called.append(kwargs["model"])
        return _success_response('{"action":"HOLD"}')

    c._client.chat.completions.create = fake_create
    out = asyncio.run(c.chat(system="s", user="u", max_tokens=64))
    assert out == '{"action":"HOLD"}'
    assert called == [c._model]  # never fell back


def test_try_fallback_false_when_primary_and_fallback_same():
    c = _make_client()
    c._fallback_model = c._model  # same model: nothing to fall back to
    assert c._try_fallback(c._model, allow_fallback=True) is False


def test_primary_called_even_after_budget_counters_high():
    """Caps removed: a healthy primary is used even if usage counters are high.

    Previously a full local token budget refused the primary locally and
    forced a fallback.  With caps removed there is no local veto, so the
    primary is called directly.
    """
    import asyncio

    c = _make_client()
    c._fallback_model = "qwen/qwen3.6-27b"
    # Fill the (now-inert) budget counters; must NOT force a fallback.
    c._record_token_usage(c._max_tokens_per_day)
    assert c.is_available() is True  # not vetoed

    called = []

    async def fake_create(**kwargs):
        called.append(kwargs["model"])
        return _success_response('{"action":"ENTER"}')

    c._client.chat.completions.create = fake_create

    out = asyncio.run(c.chat(system="s", user="u", max_tokens=64))
    assert out == '{"action":"ENTER"}'
    assert called == [c._model]  # primary used; no local budget veto
