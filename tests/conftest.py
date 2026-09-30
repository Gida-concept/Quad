"""Shared pytest fixtures for the Quad test suite."""

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("QUAD_TESTING", "1")

# Env vars that would otherwise leak a developer's real configuration into
# tests (and, worse, make a test talk to a live exchange).
for _leak in (
    "BYBIT_API_KEY",
    "BYBIT_API_SECRET",
    "GROQ_API_KEY",
    "GROQ_API_KEYS",
    "TELEGRAM_BOT_TOKEN",
    "QUAD_TRADINGVIEW_WEBHOOK_SECRET",
    "QUAD_HEALTH_API_KEY",
    "QUAD_CREDENTIAL_KEY",
    "DATABASE_URL",
):
    os.environ.pop(_leak, None)


def pytest_configure(config: pytest.Config) -> None:
    """Declare markers so ``-W error`` / strict runs do not warn on them."""
    config.addinivalue_line(
        "markers", "network: test performs real network I/O (opt-in only)"
    )
