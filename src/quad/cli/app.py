"""Quad CLI application — secondary interface for debugging, manual
commands, and maintenance operations for the futures trading bot.

Built on Typer.  Most commands are async and use asyncio.run() internally.
"""

from __future__ import annotations

import time as _time
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog
import typer

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="quad",
    help="Quad Futures Trading Bot — CLI interface",
    no_args_is_help=True,
)


# ============================================================================
# Helpers
# ============================================================================


def _load_config(config_path: str) -> dict[str, Any]:
    """Load and validate config from file + env vars.

    Parameters
    ----------
    config_path:
        Path to the local config YAML file.

    Returns
    -------
    dict
        The resolved configuration dictionary.
    """
    from quad.config.manager import ConfigManager

    config_dir = str(Path(config_path).parent.resolve())
    cm = ConfigManager(config_dir)
    return cm.to_dict()


def _print_table(
    headers: list[str],
    rows: list[list[str]],
    min_col_widths: list[int] | None = None,
) -> None:
    """Print a simple aligned table to stdout."""
    if not rows:
        return

    col_count = len(headers)
    widths = [len(h) for h in headers]

    for row in rows:
        for i, cell in enumerate(row):
            if i < col_count:
                widths[i] = max(widths[i], len(cell))

    if min_col_widths:
        for i in range(col_count):
            if i < len(min_col_widths):
                widths[i] = max(widths[i], min_col_widths[i])

    # Print header
    header_line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(header_line)
    print("-" * len(header_line))

    # Print rows
    for row in rows:
        line = "  ".join(
            cell.ljust(widths[i]) if i < len(widths) else cell
            for i, cell in enumerate(row)
        )
        print(line)


def _format_pnl(pnl: Decimal) -> str:
    """Format a PnL value with sign."""
    sign = "+" if pnl >= 0 else ""
    return f"{sign}${float(pnl):,.2f}"


# ============================================================================
# CLI Commands
# ============================================================================


@app.command()
def status(
    config_path: str = typer.Option(
        "config/config.yaml", "--config", "-c", help="Path to config YAML"
    ),
) -> None:
    """Show bot status overview."""
    config = _load_config(config_path)
    mode = config["_mode"]
    dry_run = config["_dry_run"]
    exchange_name = config["exchange"]["name"]
    testnet = config["exchange"]["testnet"]
    leverage = config["trading"]["leverage"]
    margin_mode = config["trading"]["margin_mode"]
    position_mode = config["trading"]["position_mode"]

    print("=" * 50)
    print("  QUAD FUTURES TRADING BOT — STATUS")
    print("=" * 50)
    print(f"  Mode:          {mode}")
    print(f"  Dry Run:       {dry_run}")
    print(f"  Exchange:      {exchange_name}")
    print(f"  Testnet:       {testnet}")
    print(f"  Leverage:      {leverage}x")
    print(f"  Margin Mode:   {margin_mode}")
    print(f"  Position Mode: {position_mode}")
    print(f"  Config File:   {config_path}")
    print(f"  Timestamp:     {_time.strftime('%Y-%m-%d %H:%M:%S UTC', _time.gmtime())}")
    print("=" * 50)


@app.command()
def balance(
    config_path: str = typer.Option(
        "config/config.yaml", "--config", "-c", help="Path to config YAML"
    ),
) -> None:
    """Show account balance."""
    config = _load_config(config_path)
    print(f"Account balance (from config mode: {config['_mode']})")
    print()
    print("  Use the Telegram bot `/balance` command for live data,")
    print("  or connect the exchange adapter for REST queries.")


@app.command()
def positions(
    config_path: str = typer.Option(
        "config/config.yaml", "--config", "-c", help="Path to config YAML"
    ),
) -> None:
    """List open positions (from database if available)."""
    _ = _load_config(config_path)
    print("Open Futures Positions")
    print()
    print("  Use the Telegram bot `/positions` command for live data.")
    print("  CLI position queries require a running exchange adapter.")
    print("  Columns: Symbol, Side (LONG/SHORT), Size, Entry, Mark, Liq.Px, PnL, Lev")


@app.command()
def orders(
    config_path: str = typer.Option(
        "config/config.yaml", "--config", "-c", help="Path to config YAML"
    ),
) -> None:
    """List open orders (from database if available)."""
    _ = _load_config(config_path)
    print("Open Orders")
    print()
    print("  Use the Telegram bot `/orders` command for live data.")
    print("  CLI order queries require a running execution engine.")


@app.command()
def risk(
    config_path: str = typer.Option(
        "config/config.yaml", "--config", "-c", help="Path to config YAML"
    ),
) -> None:
    """Show risk status."""
    config = _load_config(config_path)
    risk_config = config["risk"]

    print("Risk Status")
    print("=" * 50)
    print(f"  Max Positions:             {risk_config['max_positions']}")
    print(
        f"  Max Position Size:         {float(risk_config['max_position_size_pct']):.0%}"
    )
    print(f"  Max Portfolio Risk:        {risk_config['max_portfolio_risk_pct']}%")
    print(f"  Max Daily Loss:            ${risk_config['max_daily_loss_usd']}")
    print(f"  Max Drawdown:              {risk_config['max_drawdown_pct']}%")
    print(
        f"  Min Liquidation Distance:  {float(risk_config['min_distance_to_liquidation_pct']):.0%}"
    )
    print(
        f"  Liquidation Warn Fraction: {float(risk_config.get('liquidation_distance_fraction', 0.5)):.0%} of 1/leverage distance"
    )
    print(
        f"  Max Funding Rate Cost:     {float(risk_config['max_funding_rate_cost']):.4%}"
    )
    print(
        f"  Max Position Concentration: {risk_config['max_position_concentration']:.0%}"
    )
    print("=" * 50)
    print()
    print("  Use the Telegram bot `/risk` command for live risk status.")
    print("  CLI risk queries require a running risk manager.")


@app.command()
def evaluate(
    strategy_name: str = typer.Argument(..., help="Strategy name to evaluate"),
    config_path: str = typer.Option(
        "config/config.yaml", "--config", "-c", help="Path to config YAML"
    ),
) -> None:
    """Evaluate a strategy and show recommended actions."""
    config = _load_config(config_path)
    strategy_params = config["strategy"].get(strategy_name)

    from quad.strategy.base import StrategyRegistry

    cls = StrategyRegistry.get(strategy_name)
    if cls is None:
        print(f"❌ Strategy '{strategy_name}' not found in registry.")
        print(f"  Available strategies: {', '.join(StrategyRegistry.list())}")
        raise typer.Exit(code=1)

    spec = cls.get_params_spec()
    print(f"Strategy: {strategy_name}")
    print(f"  Description: {cls.get_description()}")
    print(f"  Parameters: {strategy_params or '(using defaults)'}")
    print()
    for p in spec:
        default = p.default if p.default is not None else "(required)"
        print(f"  • {p.name}: {p.description} [{p.type}] (default: {default})")
    print()
    print("To run evaluation live, use:")
    print(f"  quad execute {strategy_name}")


@app.command()
def execute(
    strategy_name: str = typer.Argument(..., help="Strategy name to execute"),
    dry_run: bool = typer.Option(
        True, "--dry-run", "-n", help="Dry run (no real orders)"
    ),
    config_path: str = typer.Option(
        "config/config.yaml", "--config", "-c", help="Path to config YAML"
    ),
) -> None:
    """Execute strategy signals (with --no-dry-run for live)."""
    _ = _load_config(config_path)

    from quad.strategy.base import StrategyRegistry

    if StrategyRegistry.get(strategy_name) is None:
        print(f"❌ Strategy '{strategy_name}' not found.")
        print(f"  Available strategies: {', '.join(StrategyRegistry.list())}")
        raise typer.Exit(code=1)

    print(f"Executing strategy: {strategy_name}")
    print(f"  Dry run: {dry_run}")
    print()
    print("Full execution requires a running orchestrator.")
    if dry_run:
        print("[DRY RUN] No orders will be placed.")
    else:
        print("[LIVE] Orders will be placed on the exchange.")


@app.command()
def backtest(
    strategy_name: str = typer.Argument(..., help="Strategy to backtest"),
    days: int = typer.Option(30, "--days", "-d", help="Number of days to backtest"),
    config_path: str = typer.Option(
        "config/config.yaml", "--config", "-c", help="Path to config YAML"
    ),
) -> None:
    """Backtest a strategy against historical data."""
    _ = _load_config(config_path)

    from quad.strategy.base import StrategyRegistry

    if StrategyRegistry.get(strategy_name) is None:
        print(f"❌ Strategy '{strategy_name}' not found.")
        print(f"  Available strategies: {', '.join(StrategyRegistry.list())}")
        raise typer.Exit(code=1)

    print(f"Backtesting strategy: {strategy_name}")
    print(f"  Period: {days} days")
    print()

    # The backtest engine requires a live database manager, a strategy
    # instance, and historical futures data.  None of these are wired up
    # yet, so fail honestly instead of pretending the run succeeded.
    print("❌ Backtesting is not implemented yet.")
    print("  Required: a configured DatabaseManager with historical futures data,")
    print("  a strategy instance, and an underlying symbol.")
    print("  See docs/strategy-development.md for the planned engine.run() API.")
    raise typer.Exit(code=1)


@app.command(name="config")
def config_view(
    config_path: str = typer.Option(
        "config/config.yaml", "--config", "-c", help="Path to config YAML"
    ),
) -> None:
    """Show current resolved configuration overview."""
    config = _load_config(config_path)

    print("Resolved Configuration")
    print("=" * 50)

    def _print_section(prefix: str, data: Any, indent: int = 0) -> None:
        pad = "  " * indent
        if isinstance(data, dict):
            print(f"{pad}{prefix}:")
            for key, value in data.items():
                _print_section(key, value, indent + 1)
        elif isinstance(data, list):
            print(f"{pad}{prefix}: {data}")
        else:
            print(f"{pad}{prefix}: {data}")

    for key, value in config.items():
        _print_section(key, value)


@app.command(name="db-info")
def db_info(
    config_path: str = typer.Option(
        "config/config.yaml", "--config", "-c", help="Path to config YAML"
    ),
) -> None:
    """Show database statistics."""
    config = _load_config(config_path)
    dsn = config["persistence"]["dsn"]

    print("Database Info")
    print("=" * 50)
    print(f"  DSN: {dsn}")
    print()
    print("  Use the Telegram bot or execute the bot in live mode")
    print("  to populate and query database statistics.")


@app.command()
def run(
    config_path: str = typer.Option(
        "config/config.yaml", "--config", "-c", help="Path to config YAML"
    ),
) -> None:
    """Run the bot (start all subsystems)."""
    _ = _load_config(config_path)
    print("Starting Quad bot...")
    print()
    print("Full bot execution requires asyncio.run() and all subsystems.")
    print("Use the Python API directly:")
    print()
    print("  from quad.bot import QuadBot")
    print("  bot = QuadBot(config)")
    print("  await bot.start()")


def main() -> None:
    """Entry point for ``quad`` CLI command."""
    app()


if __name__ == "__main__":
    main()
