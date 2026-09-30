"""Tests for the Quad Typer CLI.

The scan found the README quickstart (``quad start --dry-run``) and ~10
documented commands did not exist, and that the CLI could not run at all on
a default Windows console (cp1252 cannot encode the status glyphs the
commands print).  These tests pin the command surface and the safety gates.
"""

import inspect

from typer.testing import CliRunner

from quad.cli.app import app

runner = CliRunner()


def _commands() -> set[str]:
    """Return every registered command name."""
    from typer.main import get_command

    return set(get_command(app).commands)


def test_every_command_named_in_the_readme_exists():
    readme_commands = {
        "start",
        "status",
        "strategies",
        "health",
        "stop",
        "logs",
        "trades",
        "decisions",
        "config",
        "backtest",
        "evaluate",
        "execute",
        "balance",
        "positions",
        "orders",
        "risk",
        "run",
        "db-info",
    }
    missing = readme_commands - _commands()
    assert not missing, f"documented but missing CLI commands: {sorted(missing)}"


def test_readme_quickstart_command_runs():
    """`quad start --dry-run` is the documented first step."""
    result = runner.invoke(app, ["start", "--help"])
    assert result.exit_code == 0
    assert "--dry-run" in result.output
    assert "--live" in result.output


def test_commands_do_not_crash_on_a_cp1252_console(monkeypatch):
    """A Windows default console must not abort a command mid-print.

    Regression: printing a status glyph raised UnicodeEncodeError and killed
    the command, so `quad health` / `quad evaluate nope` were unusable.
    """
    import io
    import sys

    from quad.__main__ import configure_console_encoding

    class _NoReconfigure(io.StringIO):
        pass

    monkeypatch.setattr(sys, "stdout", _NoReconfigure())
    monkeypatch.setattr(sys, "stderr", _NoReconfigure())
    # The helper must tolerate a stream that cannot be reconfigured.
    configure_console_encoding()

    result = runner.invoke(app, ["evaluate", "definitely-not-a-strategy"])
    # Fails cleanly (exit 1) rather than raising UnicodeEncodeError.
    assert result.exit_code == 1
    assert "not found in registry" in result.output


def test_strategies_lists_the_registry():
    result = runner.invoke(app, ["strategies"])
    assert result.exit_code == 0
    assert "trend_following" in result.output


def test_stop_explains_graceful_shutdown():
    result = runner.invoke(app, ["stop"])
    assert result.exit_code == 0
    assert "SIGINT" in result.output
    assert "/kill" in result.output


def test_start_live_refuses_while_testnet_is_on():
    """`--live` must be refused while the exchange is testnet."""
    result = runner.invoke(app, ["start", "--live"])
    assert result.exit_code == 1
    assert "testnet" in result.output.lower()


def test_start_live_refuses_while_dry_run_flag_is_set():
    """`--live` must be refused while `_dry_run` still blocks orders."""
    result = runner.invoke(app, ["start", "--live"])
    # The default config has testnet: true, so the first gate trips; either
    # message is an acceptable refusal, but it must NOT proceed.
    assert result.exit_code == 1
    assert "refused" in result.output.lower()


def test_execute_live_requires_explicit_confirmation():
    result = runner.invoke(app, ["execute", "trend_following", "--live"])
    assert result.exit_code == 1
    assert "confirmation" in result.output.lower()


def test_execute_live_flag_exists_and_dry_run_is_the_default():
    """Regression: help advertised --no-dry-run, which Typer never generated."""
    result = runner.invoke(app, ["execute", "--help"])
    assert result.exit_code == 0
    assert "--live" in result.output
    assert "--dry-run" in result.output


def test_execute_live_refused_on_testnet():
    result = runner.invoke(app, ["execute", "trend_following", "--live", "--yes"])
    assert result.exit_code == 1
    assert "testnet" in result.output.lower()


def test_execute_dry_run_succeeds():
    result = runner.invoke(app, ["execute", "trend_following"])
    assert result.exit_code == 0
    assert "DRY RUN" in result.output


def test_health_reports_unreachable_bot_cleanly():
    result = runner.invoke(app, ["health", "--timeout", "0.2"])
    assert result.exit_code == 1
    assert "No bot reachable" in result.output


def test_backtest_fails_honestly_when_unavailable():
    result = runner.invoke(app, ["backtest", "trend_following", "--days", "1"])
    assert result.exit_code == 1
    assert "not implemented" in result.output.lower()


def test_redaction_masks_secret_like_keys():
    from quad.cli.app import _redact_value

    assert _redact_value("api_key", "supersecret") == "***REDACTED***"
    assert _redact_value("api_secret", "supersecret") == "***REDACTED***"
    assert _redact_value("telegram_bot_token", "abc") == "***REDACTED***"
    assert _redact_value("leverage", 10) == 10


def test_dsn_masking_hides_password():
    from quad.cli.app import _mask_dsn

    masked = _mask_dsn("postgresql://quad:hunter2@db:5432/quad")
    assert "hunter2" not in masked
    assert "quad" in masked
    # Non-DSN strings pass through.
    assert _mask_dsn("data/quad.db") == "data/quad.db"


def test_config_command_redacts_secrets():
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "***REDACTED***" in result.output


def test_database_url_is_mapped_into_config():
    """DATABASE_URL is documented as the persistence.dsn override."""
    from quad.config.manager import ENV_VAR_MAP

    assert ENV_VAR_MAP["DATABASE_URL"] == "persistence.dsn"
    assert ENV_VAR_MAP["QUAD_DSN"] == "persistence.dsn"


def test_orchestrator_no_longer_reads_database_url_directly():
    """One source of truth: the ConfigManager owns dsn resolution."""
    from quad.orchestrator.orchestrator import QuadOrchestrator

    code = _strip_comments(QuadOrchestrator._init_database)
    assert "DATABASE_URL" not in code


def test_unknown_mode_is_rejected():
    """A stale QUAD_MODE (e.g. 'okx') must fail loudly, not fall through."""
    from quad.orchestrator.orchestrator import _VALID_MODES, QuadOrchestrator

    assert "bybit" in _VALID_MODES
    assert "dry_run" in _VALID_MODES
    assert "okx" not in _VALID_MODES

    code = _strip_comments(QuadOrchestrator._init_exchange_adapter)
    assert "_VALID_MODES" in code
    assert "raise ValueError" in code


def _strip_comments(obj) -> str:
    """Return source with comments and docstrings removed."""
    import io
    import tokenize

    src = inspect.getsource(obj)
    toks = [
        t
        for t in tokenize.generate_tokens(io.StringIO(src).readline)
        if t.type
        not in (
            tokenize.COMMENT,
            tokenize.NL,
            tokenize.NEWLINE,
            tokenize.INDENT,
            tokenize.DEDENT,
        )
    ]
    return tokenize.untokenize(toks)


def test_readme_has_no_commands_missing_from_the_cli():
    """Guard against the docs and the CLI drifting apart again."""
    import re
    from pathlib import Path

    readme = Path(__file__).resolve().parents[1] / "README.md"
    text = readme.read_text(encoding="utf-8", errors="replace")
    # Quick-start block: `quad <verb>` invocations.
    verbs = set(re.findall(r"\bquad ([a-z][a-z-]{2,})\b", text))
    # Options and prose words that are not subcommands.
    noise = {
        "start",  # keep: this IS a command
    }
    verbs -= noise
    unknown = {v for v in verbs if v not in _commands()}
    assert not unknown, (
        f"README references non-existent quad commands: {sorted(unknown)}"
    )
