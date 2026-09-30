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


@app.callback()
def _main_callback() -> None:
    """Prepare the process before any command runs.

    Fixes the Windows console codepage up front: cp1252 cannot encode the
    status glyphs these commands print (``❌``, ``→``, ``🚨``), which raised
    ``UnicodeEncodeError`` and aborted the command.
    """
    from quad.__main__ import configure_console_encoding

    configure_console_encoding()


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


_SECRET_KEY_HINTS = ("secret", "password", "passwd", "token", "api_key", "private_key")


def _redact_value(key: str, value: Any) -> Any:
    """Redact credential-like config values before display."""
    if isinstance(value, str) and any(h in key.lower() for h in _SECRET_KEY_HINTS):
        return "***REDACTED***" if value else value
    return value


def _mask_dsn(dsn: str) -> str:
    """Mask the password segment of a DSN (``scheme://user:pass@host``)."""
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(dsn)
        if parts.password:
            netloc = parts.hostname or ""
            if parts.username:
                netloc = f"{parts.username}:***@{netloc}"
            if parts.port:
                netloc = f"{netloc}:{parts.port}"
            return urlunsplit(
                (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
            )
    except Exception:
        pass
    return dsn


def _start_foreground(config_path: str) -> None:
    """Configure logging and run the orchestrator until interrupted."""
    from quad.__main__ import _configure_logging

    _configure_logging()
    from quad.orchestrator import QuadOrchestrator

    orchestrator = QuadOrchestrator(config_path=config_path)
    try:
        import asyncio

        asyncio.run(orchestrator.run_forever())
    except KeyboardInterrupt:
        # run_forever() already performed the graceful shutdown.
        pass


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
    """Show the last persisted account snapshot.

    Reads the local database rather than the exchange, so it reflects the
    last completed trading cycle — it cannot show a balance that changed
    after the last write.  For live balances use the Telegram ``/balance``
    command (or Bybit directly).
    """
    from quad.persistence.repositories import AccountRepository

    config = _load_config(config_path)
    rows = _db_rows(config, AccountRepository, 5)
    if not rows:
        print("No account snapshot recorded yet.")
        print("  The account row is written by the trading cycle; run the bot first.")
        print("  Live balances: Telegram /balance, or query Bybit directly.")
        raise typer.Exit(code=1)
    for r in rows:
        print("Account snapshot")
        print("=" * 50)
        for field in (
            "id",
            "exchange",
            "total_usdt",
            "available_balance",
            "total_wallet_balance",
            "timestamp",
        ):
            value = getattr(r, field, None)
            if value not in (None, ""):
                print(f"  {field.replace('_', ' ').title():24s} {value}")
        print("=" * 50)
        break
    print("  (snapshot from the last completed cycle — not a live balance)")


@app.command()
def positions(
    config_path: str = typer.Option(
        "config/config.yaml", "--config", "-c", help="Path to config YAML"
    ),
) -> None:
    """List positions as last persisted by the trading cycle.

    Not a live exchange query: the CLI does not open an exchange session.
    For live positions use Telegram ``/positions``.
    """
    from quad.persistence.repositories import PositionRepository

    config = _load_config(config_path)
    rows = _db_rows(config, PositionRepository, 20)
    if not rows:
        print("No positions recorded in the database.")
        print("  Live positions: Telegram /positions, or Bybit directly.")
        return
    table = [
        [
            str(getattr(r, "symbol", "")),
            str(getattr(r, "side", "") or getattr(r, "position_side", "")),
            str(getattr(r, "quantity", "") or getattr(r, "size", "")),
            str(getattr(r, "entry_price", "")),
            str(getattr(r, "unrealized_pnl", "") or getattr(r, "pnl", "")),
        ]
        for r in rows
    ]
    _print_table(
        ["SYMBOL", "SIDE", "SIZE", "ENTRY", "PNL"],
        table,
        min_col_widths=[10, 6, 10, 12, 10],
    )
    print("\n  (from the database, not a live exchange query)")


@app.command()
def orders(
    config_path: str = typer.Option(
        "config/config.yaml", "--config", "-c", help="Path to config YAML"
    ),
) -> None:
    """List orders as last persisted by the trading cycle.

    Not a live exchange query.  For live orders use Telegram ``/orders``.
    """
    from quad.persistence.repositories import OrderRepository

    config = _load_config(config_path)
    rows = _db_rows(config, OrderRepository, 20)
    if not rows:
        print("No orders recorded in the database.")
        print("  Live orders: Telegram /orders, or Bybit directly.")
        return
    table = [
        [
            str(getattr(r, "timestamp", "")),
            str(getattr(r, "symbol", "")),
            str(getattr(r, "side", "")),
            str(getattr(r, "quantity", "")),
            str(getattr(r, "status", "")),
        ]
        for r in rows
    ]
    _print_table(
        ["TIMESTAMP", "SYMBOL", "SIDE", "QTY", "STATUS"],
        table,
        min_col_widths=[13, 10, 6, 8, 10],
    )
    print("\n  (from the database, not a live exchange query)")


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
        True,
        "--dry-run/--live",
        "-n",
        help="Dry run (default) or --live to permit real orders",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Confirm live execution (required with --live)"
    ),
    config_path: str = typer.Option(
        "config/config.yaml", "--config", "-c", help="Path to config YAML"
    ),
) -> None:
    """Execute strategy signals (--live for real orders)."""
    if not dry_run and not yes:
        print("❌ Live execution requires explicit confirmation.")
        print("  Re-run with `--yes` to place real orders, e.g.:")
        print(f"  quad execute {strategy_name} --live --yes")
        raise typer.Exit(code=1)

    config = _load_config(config_path)

    from quad.strategy.base import StrategyRegistry

    if StrategyRegistry.get(strategy_name) is None:
        print(f"❌ Strategy '{strategy_name}' not found.")
        print(f"  Available strategies: {', '.join(StrategyRegistry.list())}")
        raise typer.Exit(code=1)

    if not dry_run:
        testnet = bool(config.get("exchange", {}).get("testnet", True))
        if testnet:
            print("❌ --live refused: exchange.testnet is still true.")
            print("   Set `exchange.testnet: false` (or BYBIT_TESTNET=false) first.")
            raise typer.Exit(code=1)

    print(f"Executing strategy: {strategy_name}")
    print(f"  Dry run: {dry_run}")
    print()
    if dry_run:
        print("[DRY RUN] No orders will be placed.")
    else:
        print("[LIVE] Orders will be placed on the exchange.")
        print()
        print("  This command only validates the request. Strategy execution")
        print("  runs in the trading process — use `quad start`, or the")
        print("  Telegram /execute flow.")
    return


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
                _print_section(key, _redact_value(key, value), indent + 1)
        elif isinstance(data, list):
            print(f"{pad}{prefix}: {data}")
        else:
            print(f"{pad}{prefix}: {_redact_value(prefix, data)}")

    for key, value in config.items():
        _print_section(key, value)


@app.command()
def db_info(
    config_path: str = typer.Option(
        "config/config.yaml", "--config", "-c", help="Path to config YAML"
    ),
) -> None:
    """Show database statistics (row counts per table)."""
    config = _load_config(config_path)
    dsn = _mask_dsn(str(config.get("persistence", {}).get("dsn", "(not configured)")))

    print("Database Info")
    print("=" * 50)
    print(f"  DSN: {dsn}")

    from pathlib import Path

    # SQLite can be inspected without a driver; Postgres cannot.
    path = Path(dsn)
    if not dsn.startswith(("postgres://", "postgresql://")):
        if not path.exists():
            print(f"  File: {path} (not created yet — has the bot ever run?)")
            return
        print(f"  File: {path} ({path.stat().st_size / 1024:.1f} KiB)")

    try:
        rows = _table_counts(config)
    except Exception as exc:
        print(f"  Could not read row counts: {exc}")
        return
    if rows:
        _print_table(["TABLE", "ROWS"], [[t, str(n)] for t, n in rows])
    else:
        print("  (no tables found)")


def _table_counts(config: dict[str, Any]) -> list[tuple[str, int]]:
    """Return ``(table, row_count)`` for every model table."""
    import asyncio

    from quad.persistence import create_database
    from quad.persistence.models import ALL_MODELS

    async def _run() -> list[tuple[str, int]]:
        dsn = str(config.get("persistence", {}).get("dsn", "data/quad.db"))
        db = create_database(dsn)
        try:
            await db.connect()
            out: list[tuple[str, int]] = []
            for model in ALL_MODELS:
                table = getattr(model, "__tablename__", None)
                if not table:
                    continue
                try:
                    async with db.pool.acquire() as conn:
                        row = await conn.fetchrow(f"SELECT COUNT(*) AS n FROM {table}")
                    out.append((table, int(row["n"]) if row else 0))
                except Exception:
                    out.append((table, -1))
            return out
        finally:
            try:
                await db.disconnect()
            except Exception:
                pass

    return asyncio.run(_run())


@app.command()
def run(
    config_path: str = typer.Option(
        "config/config.yaml", "--config", "-c", help="Path to config YAML"
    ),
) -> None:
    """Run the bot in the foreground (all subsystems, trading loop).

    This is the real launcher.  For a one-line alias see ``quad start``.
    """
    _start_foreground(config_path)


@app.command()
def start(
    config_path: str = typer.Option(
        "config/config.yaml", "--config", "-c", help="Path to config YAML"
    ),
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--live",
        "-n",
        help="Dry run (default) or --live to permit real orders",
    ),
) -> None:
    """Start the trading bot in the foreground.

    Runs the full orchestrator (config -> database -> Bybit -> market data ->
    risk -> execution -> Telegram -> health server) and blocks until
    SIGINT/SIGTERM.  ``--dry-run`` is the default and the safe first step;
    ``--live`` removes the guard and permits real orders, so it also
    requires ``_dry_run: false`` and ``exchange.testnet: false`` in config.
    """
    if not dry_run:
        config = _load_config(config_path)
        testnet = bool(config.get("exchange", {}).get("testnet", True))
        dry_flag = config.get("_dry_run", True)
        if testnet:
            print("❌ --live refused: exchange.testnet is still true.")
            print("   Set `exchange.testnet: false` (or BYBIT_TESTNET=false) first.")
            raise typer.Exit(code=1)
        if dry_flag:
            print(
                "❌ --live refused: `_dry_run` is still true. The engine blocks "
                "every order while it is set."
            )
            print("   Set `_dry_run: false` (or QUAD_DRY_RUN=false) first.")
            raise typer.Exit(code=1)
        print("🚨 LIVE MODE: real orders will be placed on Bybit.")
    _start_foreground(config_path)


@app.command()
def stop() -> None:
    """Explain how to stop the bot.

    The bot runs in the foreground, so it stops on SIGINT (Ctrl+C) or
    SIGTERM, which triggers the orchestrator's reverse-order graceful
    shutdown.  In Docker, use ``docker compose stop quad`` (or
    ``docker stop``), which sends SIGTERM.
    """
    print("Quad runs in the foreground and stops gracefully on SIGINT/SIGTERM.")
    print()
    print("  Foreground:  press Ctrl+C")
    print("  Docker:      docker compose stop quad")
    print()
    print("A remote kill switch is available from Telegram: /kill")
    print("  (halts new entries and cancels open orders; open positions remain).")


@app.command()
def strategies() -> None:
    """List strategies available in the registry."""
    from quad.strategy.base import StrategyRegistry

    names = StrategyRegistry.list()
    if not names:
        print("No strategies are registered.")
        return
    print("Registered Strategies")
    print("=" * 60)
    for name in names:
        cls = StrategyRegistry.get(name)
        if cls is None:
            continue
        print(f"\n{name}")
        print(f"  {cls.get_description()}")
        for p in cls.get_params_spec():
            default = p.default if p.default is not None else "(required)"
            print(f"    - {p.name} ({p.type}, default: {default}): {p.description}")
    print("=" * 60)


@app.command()
def health(
    config_path: str = typer.Option(
        "config/config.yaml", "--config", "-c", help="Path to config YAML"
    ),
    timeout: float = typer.Option(3.0, "--timeout", help="HTTP timeout in seconds"),
) -> None:
    """Query the running bot's health endpoint."""
    import json
    import urllib.error
    import urllib.request

    config = _load_config(config_path)
    port = int(config.get("monitoring", {}).get("health_server", {}).get("port", 9090))
    bind = str(
        config.get("monitoring", {})
        .get("health_server", {})
        .get("bind_address", "127.0.0.1")
    )
    if bind in ("0.0.0.0", "::"):
        bind = "127.0.0.1"

    url = f"http://{bind}:{port}/health"
    api_key = str(
        config.get("monitoring", {}).get("health_server", {}).get("api_key", "")
    )
    req = urllib.request.Request(url)
    if api_key:
        req.add_header("X-API-Key", api_key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.URLError as exc:
        print(f"❌ No bot reachable at {url} ({exc.reason}).")
        print("   The bot may not be running, or the health server is disabled.")
        raise typer.Exit(code=1) from exc

    print(f"Health ({url})")
    print("=" * 50)
    print(f"  status:   {payload.get('status')}")
    print(f"  uptime:   {payload.get('uptime')}s")
    print(f"  version:  {payload.get('version')}")
    for name, ok in (payload.get("components") or {}).items():
        print(f"  {'OK  ' if ok else 'FAIL'} {name}")
    degraded = payload.get("degraded") or []
    if degraded:
        print(f"  degraded: {', '.join(degraded)}")
        raise typer.Exit(code=1)


def _db_rows(config: dict[str, Any], repo_cls: Any, limit: int) -> list[Any]:
    """Read the most recent rows from a repository (best effort)."""

    async def _run() -> list[Any]:
        from quad.persistence import create_database
        from quad.persistence.repositories import make_repo

        dsn = str(config.get("persistence", {}).get("dsn", "data/quad.db"))
        db = create_database(dsn)
        try:
            await db.connect()
            await db.initialize()
            repo = make_repo(repo_cls, db, config)
            return await repo.get_recent(limit=limit)
        finally:
            try:
                await db.disconnect()
            except Exception:
                pass

    import asyncio

    try:
        return asyncio.run(_run())
    except FileNotFoundError:
        print("❌ Database file not found. Has the bot ever run?")
        raise typer.Exit(code=1) from None
    except Exception as exc:
        print(f"❌ Could not read the database: {exc}")
        raise typer.Exit(code=1) from None


@app.command()
def trades(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of rows"),
    config_path: str = typer.Option(
        "config/config.yaml", "--config", "-c", help="Path to config YAML"
    ),
) -> None:
    """Show recent trades from the local database."""
    from quad.persistence.repositories import TradeRepository

    config = _load_config(config_path)
    rows = _db_rows(config, TradeRepository, limit)
    if not rows:
        print("No trades recorded yet.")
        return
    table = [
        [
            str(getattr(r, "timestamp", "")),
            str(getattr(r, "symbol", "")),
            str(getattr(r, "side", "")),
            str(getattr(r, "quantity", "")),
            str(getattr(r, "price", "")),
            str(getattr(r, "pnl", "")),
        ]
        for r in rows
    ]
    _print_table(
        ["TIMESTAMP", "SYMBOL", "SIDE", "QTY", "PRICE", "PNL"],
        table,
        min_col_widths=[13, 10, 6, 8, 12, 8],
    )


@app.command()
def decisions(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of rows"),
    config_path: str = typer.Option(
        "config/config.yaml", "--config", "-c", help="Path to config YAML"
    ),
) -> None:
    """Show recent AI/strategy decisions from the local database."""
    from quad.persistence.repositories import DecisionRepository

    config = _load_config(config_path)
    rows = _db_rows(config, DecisionRepository, limit)
    if not rows:
        print("No decisions recorded yet.")
        return
    table = [
        [
            str(getattr(r, "timestamp", "")),
            str(getattr(r, "symbol", "") or getattr(r, "contract", "")),
            str(getattr(r, "action", "")),
            str(getattr(r, "outcome", "")),
            str(getattr(r, "confidence", "")),
        ]
        for r in rows
    ]
    _print_table(
        ["TIMESTAMP", "SYMBOL", "ACTION", "OUTCOME", "CONF"],
        table,
        min_col_widths=[13, 10, 8, 10, 6],
    )


@app.command()
def logs(
    path: str = typer.Option("logs/quad.log", "--path", "-p", help="Log file"),
    lines: int = typer.Option(50, "--lines", "-n", help="How many lines"),
) -> None:
    """Print the tail of the bot's log file."""
    p = Path(path)
    if not p.exists():
        print(f"No log file at {p}.")
        print(
            "Logs go to stdout (captured by the container runtime in Docker), "
            "not to a file, unless QUAD_LOG_FILE is configured."
        )
        raise typer.Exit(code=1)
    content = p.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = content[-lines:] if lines > 0 else content
    if not tail:
        print(f"{p} is empty.")
        return
    for line in tail:
        print(line)


def main() -> None:
    """Entry point for ``quad`` CLI command."""
    app()


if __name__ == "__main__":
    main()
