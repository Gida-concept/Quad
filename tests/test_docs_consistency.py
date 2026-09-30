"""Docs-vs-code consistency tests.

The full-codebase scan found substantial documentation drift: the README
advertised a `quad start` command that did not exist, the schema table
count was wrong by four, `/pnl` was documented but nonexistent, the
strategy example used a class and method that do not exist, and risk
config keys in the docs were silently dropped by the pydantic schema
(`extra="ignore"`).

These tests pin the facts that drifted, so the docs cannot silently go
stale again.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOCS = REPO / "docs"


def _read(name: str) -> str:
    return (REPO / name).read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Telegram commands
# ---------------------------------------------------------------------------


def _registered_commands() -> set[str]:
    """Every command the bot actually answers, including the conversation flow."""
    import inspect

    from quad.bot.bot import QuadBot

    src = inspect.getsource(QuadBot._setup_handlers)
    block = src.split("_command_names = [", 1)[1].split("]", 1)[0]
    commands = set(re.findall(r'"([a-z_]+)"', block))
    # /execute is registered as a ConversationHandler, not in that list.
    commands.add("execute")
    return commands


def _documented_commands(text: str) -> set[str]:
    """Parse `/cmd`, `` `/cmd <arg>` ``, `` `/cmd` `` table rows."""
    return set(re.findall(r"^\| `/([a-z_]+)[` ]", text, re.M))


def test_every_documented_telegram_command_exists():
    documented = _documented_commands(_read("README.md"))
    assert documented, "no telegram commands parsed from README"
    unknown = documented - _registered_commands()
    assert not unknown, (
        f"README documents nonexistent Telegram commands: {sorted(unknown)}"
    )


def test_registered_telegram_commands_are_documented():
    """A registered command nobody documented is a support gap."""
    documented = _documented_commands(_read("README.md"))
    undocumented = _registered_commands() - documented
    assert not undocumented, (
        f"registered but undocumented Telegram commands: {sorted(undocumented)}"
    )


def test_no_pnl_command_documented():
    """/pnl was documented for the whole options era but never implemented."""
    assert "/pnl" not in _read("README.md")
    assert "/pnl" not in _read("docs/architecture.md")


# ---------------------------------------------------------------------------
# Persistence schema
# ---------------------------------------------------------------------------


def test_documented_table_count_matches_models():
    from quad.persistence.models import ALL_MODELS

    actual = len(ALL_MODELS)
    for name in ("README.md", "docs/architecture.md"):
        text = _read(name)
        # The stale docs said "16-table schema".
        assert "16-table" not in text, f"{name} still claims a 16-table schema"
        assert "16 tables" not in text, f"{name} still claims 16 tables"
        assert f"{actual} tables" in text or f"{actual}-table" in text, (
            f"{name} should state the real table count ({actual})"
        )


def test_no_nonexistent_tables_documented():
    """`contracts` and `stats` were documented but never existed."""
    for name in ("README.md", "docs/architecture.md"):
        text = _read(name).lower()
        assert "futures_contracts" not in text
        assert not re.search(r"\bcontracts, stats\b", text)


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


def _cli_commands() -> set[str]:
    from typer.main import get_command

    from quad.cli.app import app

    return set(get_command(app).commands)


@pytest.mark.parametrize(
    "doc",
    [
        "README.md",
        "docs/interface-commands.md",
        "docs/deployment.md",
        "docs/troubleshooting.md",
    ],
)
def test_no_doc_mentions_of_nonexistent_cli_commands(doc):
    """Catch `quad <verb>` invocations in docs that do not exist.

    Also flags real flags that were advertised but never generated (e.g.
    `quad execute --no-dry-run`, `quad start --log-level`), since those
    abort with exit code 2.
    """
    text = _read(doc)
    commands = _cli_commands()

    # Options belonging to a following flag, not subcommands.
    flag_tokens = {
        "--dry-run",
        "--live",
        "--no-dry-run",
        "--config",
        "-c",
        "--limit",
        "-n",
        "--days",
        "-d",
        "--yes",
        "-y",
        "--timeout",
        "--path",
        "-p",
        "--lines",
        "--log-level",
        "--level",
        "--follow",
        "--format",
    }
    unknown = set()
    for line in text.splitlines():
        # A backticked mention is a reference, not an invocation — this skips
        # prose corrections like "there is no `quad cancel`".
        spans = [m.span() for m in re.finditer(r"`[^`]*`", line)]
        for m in re.finditer(r"\bquad ([a-z][a-z-]{2,})\b", line):
            if any(start <= m.start() < end for start, end in spans):
                continue
            verb = m.group(1)
            if verb in flag_tokens:
                continue
            if verb not in commands:
                unknown.add(verb)
    assert not unknown, f"{doc} references nonexistent quad commands: {sorted(unknown)}"


@pytest.mark.parametrize(
    "doc",
    [
        "README.md",
        "docs/interface-commands.md",
        "docs/deployment.md",
        "docs/troubleshooting.md",
    ],
)
def test_no_shell_invocation_of_a_nonexistent_cli_command(doc):
    """Stronger variant: only look inside fenced ```bash blocks.

    The prose-scanning test above can match English text ("should show quad
    with version"); this one sees only real shell commands.
    """
    from typer.main import get_command

    from quad.cli.app import app

    commands = set(get_command(app).commands)
    # Lines that are comments, not invocations.
    for block in re.findall(r"```bash\n(.*?)```", _read(doc), re.S):
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(
                r"(?:docker compose logs\s+\w+\s*\|\s*grep\s+.*)|(quad\s+([a-z][a-z-]*))",
                line,
            )
            if not m or not m.group(2):
                continue
            verb = m.group(2)
            assert verb in commands, (
                f"{doc} shows a shell invocation of nonexistent `quad {verb}`"
            )


def test_no_documented_flags_that_do_not_exist():
    """Flags promised in docs must be on the real command's options."""
    from typer.main import get_command

    from quad.cli.app import app

    root = get_command(app)
    # `quad <verb> --flag` occurrences in the docs.
    for doc in ("README.md", "docs/interface-commands.md", "docs/deployment.md"):
        for verb, flag in re.findall(
            r"\bquad ([a-z][a-z-]{2,})\b[^`\n|]{0,40}?(--[a-z][a-z-]+)", _read(doc)
        ):
            cmd = root.commands.get(verb)
            if cmd is None:
                continue
            real = {o for p in cmd.params for o in getattr(p, "opts", [])}
            real |= {o for p in cmd.params for o in getattr(p, "secondary_opts", [])}
            # Subcommand help text is quoted separately; only assert on
            # the command's own parsed options.
            if flag not in real:
                pytest.fail(
                    f"{doc} documents `quad {verb} {flag}` but that option "
                    f"does not exist. Real options: {sorted(real)}"
                )


# ---------------------------------------------------------------------------
# Strategy development guide
# ---------------------------------------------------------------------------


def test_strategy_guide_uses_the_real_api():
    from quad.strategy.base import StrategyBase

    text = _read("docs/strategy-development.md")
    assert "StrategyBase" in text, "must reference the real base class"
    assert "quad.strategy.base" in text
    # The old guide taught a nonexistent class + method.
    assert "from quad.strategy.base import Strategy\n" not in text
    assert "def analyze(" not in text, "the real method is evaluate()"
    assert "def evaluate(" in text
    # Real param names.
    assert "ema_fast" not in text and "ema_slow" not in text, (
        "schema uses fast_ema / slow_ema"
    )
    assert StrategyBase.__name__ == "StrategyBase"


def test_strategy_guide_uses_real_action_types():
    from quad.types.risk import Action

    text = _read("docs/strategy-development.md")
    # The `type` values the engine actually branches on.
    for action_type in ("ENTER", "EXIT"):
        assert action_type in text, f"{action_type} action type must be documented"
    # Legacy aliases the engine still accepts.
    assert "open_long" in text and "open_short" in text
    assert "stop_loss_price" in text and "take_profit_price" in text
    assert hasattr(Action, "stop_loss_price")


# ---------------------------------------------------------------------------
# Risk configuration keys
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "max_portfolio_risk_pct",
        "max_daily_loss_usd",
        "max_drawdown_pct",
        "correlation_threshold_pct",
        "max_leverage",
        "per_position_sl",
        "per_position_tp",
    ],
)
def test_documented_risk_keys_exist_in_schema(key):
    from quad.config.schema import RiskConfig

    assert key in RiskConfig.model_fields, f"docs reference missing risk key: {key}"


def test_no_obsolete_risk_keys_in_docs():
    """These were documented but silently dropped by `extra="ignore"`.

    Only the YAML *key* position counts — a doc may legitimately mention the
    old name in a "not X" correction note.
    """
    obsolete = [
        "max_portfolio_risk",  # -> max_portfolio_risk_pct
        "max_daily_loss",  # -> max_daily_loss_usd
        "max_drawdown",  # -> max_drawdown_pct
        "max_correlation",  # -> correlation_threshold_pct
        "fixed_loss_per_contract",
        "target_pnl_",
    ]
    for doc in ("docs/risk-management.md", "docs/configuration.md"):
        text = _read(doc)
        # Keys as they appear in a YAML block: `  key:` at end of line.
        yaml_keys = set(re.findall(r"^\s*([a-z_]+):", text, re.M))
        for key in obsolete:
            assert key not in yaml_keys, (
                f"{doc} still sets the non-existent risk key {key!r}; it is "
                "silently dropped by the pydantic schema"
            )


def test_documented_risk_yaml_keys_all_validate():
    """Every risk key the docs set must survive real schema validation."""
    from quad.config.schema import RiskConfig

    text = _read("docs/risk-management.md")
    block = re.search(r"```yaml\n(.*?max_positions.*?)```", text, re.S)
    assert block, "no risk YAML sample found in docs/risk-management.md"
    # Only the keys directly under `risk:` — nested sub-models
    # (per_position_sl, circuit_breakers, ...) have their own schemas.
    keys = set(re.findall(r"^  ([a-z_]+):", block.group(1), re.M))
    unknown = keys - set(RiskConfig.model_fields)
    assert not unknown, (
        f"docs/risk-management.md sample sets keys absent from RiskConfig "
        f"(silently ignored): {sorted(unknown)}"
    )


# ---------------------------------------------------------------------------
# Config: no invented sections
# ---------------------------------------------------------------------------


def test_no_documented_config_section_that_does_not_exist():
    from quad.config.schema import QuadConfig

    text = _read("docs/configuration.md")
    # `logging` was a documented top-level section with no schema behind it.
    assert not re.search(r"^logging:", text, re.M), (
        "QuadConfig has no `logging` section; it is silently ignored"
    )
    assert "logging" not in QuadConfig.model_fields


def test_configuration_doc_uses_real_market_data_shape():
    """Scope the check to the `market_data:` block only.

    `order_book_depth` IS a real key elsewhere (under `ai.prompt`), so a
    document-wide substring search gives false positives.
    """
    from quad.config.schema import MarketDataConfig

    text = _read("docs/configuration.md")
    block = re.search(r"^market_data:\n((?:[ \t]+.*\n|\n)*)", text, re.M)
    assert block, "no market_data block found in docs/configuration.md"
    # Top-level keys under `market_data:` only; the sub-blocks
    # (buffer_sizes / cache_ttl / websocket / backoff) have their own models.
    keys = set(re.findall(r"^  ([a-z_]+):", block.group(1), re.M))
    known = set(MarketDataConfig.model_fields)
    unknown = keys - known
    assert not unknown, (
        f"docs/configuration.md sets market_data keys absent from "
        f"MarketDataConfig (silently ignored): {sorted(unknown)}"
    )


# ---------------------------------------------------------------------------
# Strategy plugin registration is honestly documented
# ---------------------------------------------------------------------------


def test_plugin_guide_does_not_claim_entry_points_are_wired():
    text = _read("docs/strategy-development.md")
    src = "".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in (REPO / "src").rglob("*.py")
    )
    if "importlib.metadata" not in src:
        # Nothing reads the entry-point group, so the docs must say so
        # rather than presenting it as the registration mechanism.
        assert (
            "reserved" in text.lower()
            or "not wired" in text.lower()
            or ("subclass" in text.lower())
        ), (
            "the entry-point group is declared but never read; the guide must "
            "not present it as the working registration path"
        )


# ---------------------------------------------------------------------------
# Health endpoints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("doc", ["docs/api.md", "docs/deployment.md"])
def test_documented_health_paths_exist(doc):
    import inspect

    from quad.monitoring.health import HealthServer

    src = inspect.getsource(HealthServer.start)
    text = _read(doc)
    for path in re.findall(r"^\| GET \| `(/[a-z]+)`", text, re.M):
        if path in ("/",):
            continue
        assert f'add_get("{path}"' in src, (
            f"{doc} documents GET {path}, which the health server does not serve"
        )


def test_health_long_names_are_documented():
    """The long names are canonical; short aliases are extra."""
    import inspect

    from quad.monitoring.health import HealthServer

    src = inspect.getsource(HealthServer.start)
    text = _read("docs/api.md")
    for path in ("/readiness", "/liveness", "/metrics", "/health"):
        assert f'add_get("{path}"' in src
        assert path in text, f"docs/api.md should document {path}"
