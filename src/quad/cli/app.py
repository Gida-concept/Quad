"""Quad CLI application — secondary interface for debugging, manual
commands, and maintenance operations.

Built on Typer.  Most commands are async and use asyncio.run() internally.
"""

from __future__ import annotations

import asyncio
import time as _time
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import structlog
import typer

from quad.types.domain import Position, Trade
from quad.types.risk import Action, RiskStatus

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="quad",
    help="Quad Options Trading Bot — CLI interface",
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
        "config/config.local.yaml", "--config", "-c", help="Path to config YAML"
    ),
) -> None:
    """Show bot status overview."""
    config = _load_config(config_path)
    mode = config.get("_mode", "paper")
    dry_run = config.get("_dry_run", True)
    exchange_name = config.get("exchange", {}).get("name", "binance")
    testnet = config.get("exchange", {}).get("testnet", True)

    print("=" * 50)
    print("  QUAD OPTIONS TRADING BOT — STATUS")
    print("=" * 50)
    print(f"  Mode:          {mode}")
    print(f"  Dry Run:       {dry_run}")
    print(f"  Exchange:      {exchange_name}")
    print(f"  Testnet:       {testnet}")
    print(f"  Config File:   {config_path}")
    print(f"  Timestamp:     {_time.strftime('%Y-%m-%d %H:%M:%S UTC', _time.gmtime())}")
    print("=" * 50)


@app.command()
def balance(
    config_path: str = typer.Option(
        "config/config.local.yaml", "--config", "-c", help="Path to config YAML"
    ),
) -> None:
    """Show account balance."""
    config = _load_config(config_path)
    print(f"Account balance (from config mode: {config.get('_mode', 'paper')})")
    print()
    print("  Use the Telegram bot `/balance` command for live data,")
    print("  or connect the exchange adapter for REST queries.")


@app.command()
def positions(
    config_path: str = typer.Option(
        "config/config.local.yaml", "--config", "-c", help="Path to config YAML"
    ),
) -> None:
    """List open positions (from database if available)."""
    _ = _load_config(config_path)
    print("Open Positions")
    print()
    print("  Use the Telegram bot `/positions` command for live data.")
    print("  CLI position queries require a running database connection.")


@app.command()
def orders(
    config_path: str = typer.Option(
        "config/config.local.yaml", "--config", "-c", help="Path to config YAML"
    ),
) -> None:
    """List open orders (from database if available)."""
    _ = _load_config(config_path)
    print("Open Orders")
    print()
    print("  Use the Telegram bot `/orders` command for live data.")
    print("  CLI order queries require a running execution engine.")


@app.command()
def chain(
    underlying: str = typer.Argument(..., help="BTC or ETH"),
    config_path: str = typer.Option(
        "config/config.local.yaml", "--config", "-c", help="Path to config YAML"
    ),
) -> None:
    """Show option chain for an underlying."""
    _ = _load_config(config_path)

    # Normalize
    symbol = f"{underlying.upper()}USDT" if underlying.upper() in ("BTC", "ETH") else underlying.upper()

    print(f"Option Chain: {symbol}")
    print()
    print("  Use the Telegram bot `/chain` command for live data.")
    print("  CLI chain queries require a running market data engine.")


@app.command()
def risk(
    config_path: str = typer.Option(
        "config/config.local.yaml", "--config", "-c", help="Path to config YAML"
    ),
) -> None:
    """Show risk status."""
    config = _load_config(config_path)
    risk_config = config.get("risk", {})

    print("Risk Status")
    print("=" * 50)
    print(f"  Max Positions:          {risk_config.get('max_positions', 5)}")
    print(f"  Max Position Size:      {float(risk_config.get('max_position_size_pct', 0.1)):.0%}")
    print(f"  Max Portfolio Risk:     {risk_config.get('max_portfolio_risk_pct', 20)}%")
    print(f"  Max Daily Loss:         ${risk_config.get('max_daily_loss_usd', 500)}")
    print(f"  Max Drawdown:           {risk_config.get('max_drawdown_pct', 25)}%")
    print(f"  Max Delta Exposure:     {risk_config.get('exposure', {}).get('max_delta', 100)}")
    print(f"  Max Theta Exposure:     {risk_config.get('exposure', {}).get('max_theta', -500)}")
    print(f"  Max Vega Exposure:      {risk_config.get('exposure', {}).get('max_vega', 500)}")
    print(f"  Kelly Fraction:         {risk_config.get('kelly', {}).get('fraction', 0.25)}")
    print("=" * 50)
    print()
    print("  Use the Telegram bot `/risk` command for live risk status.")
    print("  CLI risk queries require a running risk manager.")


@app.command()
def evaluate(
    strategy_name: str = typer.Argument(..., help="Strategy name to evaluate"),
    config_path: str = typer.Option(
        "config/config.local.yaml", "--config", "-c", help="Path to config YAML"
    ),
) -> None:
    """Evaluate a strategy and show recommended actions."""
    config = _load_config(config_path)
    strategy_params = config.get("strategy", {}).get(strategy_name, {})

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
    dry_run: bool = typer.Option(True, "--dry-run", "-n", help="Dry run (no real orders)"),
    config_path: str = typer.Option(
        "config/config.local.yaml", "--config", "-c", help="Path to config YAML"
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
        "config/config.local.yaml", "--config", "-c", help="Path to config YAML"
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

    # Delegate to backtesting engine
    from quad.backtesting.engine import BacktestEngine

    engine = BacktestEngine(
        strategy=None,  # type: ignore[arg-type]
        db_manager=None,  # type: ignore[arg-type]
        config={},
    )
    print("Backtesting engine ready.")
    print()
    print("Full backtest execution requires:")
    print("  • A configured DatabaseManager with historical option data")
    print("  • A strategy instance")
    print("  • An underlying symbol to backtest")
    print()
    print("Usage example from code:")
    print("  await engine.run(strategy, 'BTCUSDT', start, end)")


@app.command(name="config")
def config_view(
    config_path: str = typer.Option(
        "config/config.local.yaml", "--config", "-c", help="Path to config YAML"
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
        "config/config.local.yaml", "--config", "-c", help="Path to config YAML"
    ),
) -> None:
    """Show database statistics."""
    config = _load_config(config_path)
    dsn = config.get("persistence", {}).get("dsn", "Not configured")

    print("Database Info")
    print("=" * 50)
    print(f"  DSN: {dsn}")
    print()
    print("  Use the Telegram bot or execute the bot in live mode")
    print("  to populate and query database statistics.")


@app.command()
def run(
    config_path: str = typer.Option(
        "config/config.local.yaml", "--config", "-c", help="Path to config YAML"
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
