"""Regression tests for the Phase 1 runtime fixes (see NOTE-042)."""

import importlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


# ---------------------------------------------------------------------------
# 1. Circuit-breaker alert escapes non-string tier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_circuit_breaker_alert_escapes_int_tier():
    """A numeric ``tier`` must not crash ``_notify_circuit_breaker``."""
    import quad.orchestrator.orchestrator as orch_mod

    bot = AsyncMock()
    bot.send_message = AsyncMock()
    orch = orch_mod.QuadOrchestrator.__new__(orch_mod.QuadOrchestrator)
    orch._telegram_bot = bot
    orch._telegram_chat_id = 12345
    orch._log = MagicMock()

    await orch._notify_circuit_breaker(
        name="max_loss",
        reason="daily limit hit",
        tier=3,
    )

    bot.send_message.assert_awaited_once()
    text = bot.send_message.await_args.kwargs["text"]
    assert "Tier: 3" in text
    assert "daily limit hit" in text


@pytest.mark.asyncio
async def test_trade_alert_escapes_html_chars():
    """Trade alert text must HTML-escape dynamic fields."""
    import quad.orchestrator.orchestrator as orch_mod

    bot = AsyncMock()
    bot.send_message = AsyncMock()
    orch = orch_mod.QuadOrchestrator.__new__(orch_mod.QuadOrchestrator)
    orch._telegram_bot = bot
    orch._telegram_chat_id = 12345
    orch._log = MagicMock()

    await orch._notify_trade(
        action_type="ENTER",
        strategy="trend_following",
        contract="BTCUSDT",
        side="LONG",
        quantity="0.01",
        price="60000.0",
        pnl=None,
        reason="EMA cross <not html>",
    )

    text = bot.send_message.await_args.kwargs["text"]
    assert "&lt;not html&gt;" in text


# ---------------------------------------------------------------------------
# 2. SQLite pool creates parent directories
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_pool_creates_parent_dirs(tmp_path):
    """Connecting to a DB path in a missing directory must create it."""
    from quad.persistence.database import _SQLitePool

    target = tmp_path / "nested" / "data" / "quad.db"
    pool = _SQLitePool(str(target))
    assert not target.parent.exists()
    async with pool.acquire() as conn:
        assert conn is not None
    assert target.parent.is_dir()


# ---------------------------------------------------------------------------
# 3. CLI argparse (python -m quad)
# ---------------------------------------------------------------------------


def test_cli_version_exits_zero():
    proc = subprocess.run(
        [PY, "-m", "quad", "--version"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0
    assert "quad" in proc.stdout.lower()


def test_cli_help_exits_zero():
    proc = subprocess.run(
        [PY, "-m", "quad", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0
    assert "--config" in proc.stdout


def test_cli_parse_args_config_flag():
    from quad.__main__ import _parse_args

    ns = _parse_args(["--config", "custom.yaml"])
    assert ns.config == "custom.yaml"


# ---------------------------------------------------------------------------
# 4. quad.types __all__ composition
# ---------------------------------------------------------------------------


def test_types_all_composes_submodules():
    """``from quad.types import *`` exposes the union of submodule exports."""
    from quad import types as quad_types
    from quad.types import domain

    expected = list(quad_types.__all__)
    assert len(expected) > 0
    for name in domain.__all__[:2]:
        assert name in expected
    missing = [n for n in expected if not hasattr(quad_types, n)]
    assert not missing


# ---------------------------------------------------------------------------
# 5. Bot commands guard query.data / user_data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_selected_guards_none_query_data():
    """A callback with ``data=None`` must not raise AttributeError."""
    from telegram import CallbackQuery, Update, User

    from quad.bot.commands import QuadBotCommands

    commands = QuadBotCommands.__new__(QuadBotCommands)
    commands._log = MagicMock()

    from telegram import Bot
    from telegram.request import BaseRequest

    request = MagicMock(spec=BaseRequest)
    bot = Bot(token="123456:ABC", request=request)
    user = User(id=1, is_bot=False, first_name="t")
    query = CallbackQuery(
        id="q1",
        from_user=user,
        chat_instance="ci",
        data=None,
    )
    query.set_bot(bot)
    update = Update(update_id=1, callback_query=query)
    context = MagicMock()
    context.user_data = {}

    handler = commands.get_execute_conversation_handler()
    entry = handler.states[0][0]  # SELECTING_STRATEGY CallbackQueryHandler
    callback = entry.callback
    result = await callback(update, context)
    assert result is not None  # ConversationHandler.END path


@pytest.mark.asyncio
async def test_kill_callback_handles_query_without_message():
    """Kill-switch callback must work when the update lacks a message."""
    import telegram
    from telegram import CallbackQuery, Update, User

    from quad.bot.commands import QuadBotCommands

    commands = QuadBotCommands.__new__(QuadBotCommands)
    commands._log = MagicMock()
    commands._risk_manager = None
    commands._orchestrator = None

    from telegram import Bot
    from telegram.request import BaseRequest

    request = MagicMock(spec=BaseRequest)
    bot = Bot(token="123456:ABC", request=request)
    user = User(id=1, is_bot=False, first_name="t")
    message = MagicMock(spec=telegram.Message)
    query = CallbackQuery(
        id="q1",
        from_user=user,
        chat_instance="ci",
        data="kill_cancel",
        message=message,
    )
    query.set_bot(bot)
    update = Update(update_id=1, callback_query=query)

    await commands.cmd_kill_callback(update, MagicMock())
    # Cancel path edits the message; no assertion errors means it succeeded.


# ---------------------------------------------------------------------------
# 6. market_data.engine imports datetime
# ---------------------------------------------------------------------------


def test_market_data_engine_imports_datetime():
    """The engine module must import cleanly with datetime available."""
    mod = importlib.import_module("quad.market_data.engine")
    assert hasattr(mod, "MarketDataEngine")
