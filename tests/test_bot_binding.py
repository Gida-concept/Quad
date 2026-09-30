"""Tests for Phase-3 Telegram binding: pairing-code /start + per-command gate."""

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quad.bot.bot import QuadBot  # noqa: E402
from quad.bot.commands import QuadBotCommands  # noqa: E402
from quad.persistence import DatabaseManager  # noqa: E402
from quad.persistence.models import PairingCodeModel  # noqa: E402
from quad.persistence.repositories import (  # noqa: E402
    PairingCodeRepository,
    TelegramBindingRepository,
    TenantRepository,
)


def _run(coro):
    return asyncio.run(coro)


def _update(chat_id=999, args=None):
    message = MagicMock()
    message.chat_id = chat_id
    message.reply_text = AsyncMock()
    user = MagicMock()
    user.id = 4242
    update = MagicMock()
    update.message = message
    update.effective_user = user
    context = MagicMock()
    context.args = args or []
    return update, context, message


def _commands(db):
    return QuadBotCommands(
        {
            "config": {},
            "telegram_config": {},
            "orchestrator": None,
            "risk_manager": None,
            "execution_engine": None,
            "market_data_engine": None,
            "db_manager": db,
            "groq_client": None,
            "optimizer": None,
            "notification_chat_id": None,
        }
    )


def test_start_unbound_explains_pairing():
    async def go():
        async with DatabaseManager(":memory:") as db:
            cmds = _commands(db)
            update, context, message = _update()
            await cmds.cmd_start(update, context)
            text = message.reply_text.await_args[0][0]
            assert "isn't linked" in text and "/start YOURCODE" in text
            assert await cmds.get_bound_tenant(999) is None

    _run(go())


def test_start_pairing_code_binds():
    async def go():
        async with DatabaseManager(":memory:") as db:
            tenants = TenantRepository(db)
            t = await tenants.create_tenant("tenant-aaa")
            now = int(time.time() * 1000)
            await PairingCodeRepository(db).create(
                PairingCodeModel(
                    id=0,
                    tenant_uuid=t.tenant_uuid,
                    code="ABCD1234",
                    expires_at=now + 600_000,
                    created_at=now,
                )
            )
            cmds = _commands(db)
            update, context, message = _update(args=["abcd1234"])  # lowercase ok
            await cmds.cmd_start(update, context)
            text = message.reply_text.await_args[0][0]
            assert "linked" in text
            assert await cmds.get_bound_tenant(999) == "tenant-aaa"
            # code consumed
            assert await PairingCodeRepository(db).get_valid("ABCD1234", now) is None

    _run(go())


def test_start_bad_code_rejected():
    async def go():
        async with DatabaseManager(":memory:") as db:
            cmds = _commands(db)
            update, context, message = _update(args=["NOPE1234"])
            await cmds.cmd_start(update, context)
            assert "invalid or expired" in message.reply_text.await_args[0][0]

    _run(go())


def test_no_db_mode_stays_open():
    cmds = _commands(None)
    assert asyncio.run(cmds.get_bound_tenant(123)) == "__operator__"


def test_wrapper_gate_blocks_unbound():
    async def go():
        async with DatabaseManager(":memory:") as db:
            bot = QuadBot({"telegram": {"bot_token": "x"}}, db_manager=db)
            handler = AsyncMock()
            wrapped = bot._rate_limit_wrapper("status", handler)
            update, context, message = _update()
            await wrapped(update, context)
            handler.assert_not_awaited()
            assert "isn't linked" in message.reply_text.await_args[0][0]

            # bound chat passes through (fresh command name dodges the cooldown)
            tenants = TenantRepository(db)
            t = await tenants.create_tenant("tenant-aaa")
            await TelegramBindingRepository(db).bind(t.tenant_uuid, 999)
            wrapped2 = bot._rate_limit_wrapper("positions", handler)
            await wrapped2(update, context)
            handler.assert_awaited_once()

    _run(go())


def test_wrapper_operator_bypass():
    async def go():
        async with DatabaseManager(":memory:") as db:
            bot = QuadBot(
                {"telegram": {"bot_token": "x", "notification_chat_id": 777}},
                db_manager=db,
            )
            bot._commands._notification_chat_id = 777
            handler = AsyncMock()
            wrapped = bot._rate_limit_wrapper("status", handler)
            update, context, message = _update(chat_id=777)
            await wrapped(update, context)
            handler.assert_awaited_once()

    _run(go())
