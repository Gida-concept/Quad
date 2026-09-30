# Troubleshooting Guide

---

## Quick Reference

| Symptom | Likely Cause | First Action |
|---|---|---|
| Bot won't start | Missing dependencies or config | `quad --version` and `quad config` |
| No positions opening | Risk gate rejecting | `quad risk` to check gate status |
| Orders not executing | Exchange connectivity or rate limits | `quad health` to check connections |
| Bot not responding in Telegram | Invalid bot token or polling issue | Check `TELEGRAM_BOT_TOKEN` in `.env` |
| Telegram commands not working | Wrong chat ID or authentication | Verify `TELEGRAM_NOTIFICATION_CHAT_ID` in `.env` |
| Database errors | Corruption or disk full | `quad health` and check disk space |
| WebSocket disconnects | Network or rate limits | Check logs and exchange status |
| High cycle time | Too many open positions or data | `quad health` check cycle_time_ms |

---

## Bot Fails to Start

### Symptom: `quad start` exits immediately

**Check 1: Python version**
```bash
python --version
# Must be 3.10 or later (3.10/3.12/3.13 are tested in CI)
```

**Check 2: Dependencies installed**
```bash
pip show quad
# Should report a version
```

**Check 3: Config directory**
```bash
ls -la config/config.yaml
# Must exist and be valid YAML
```

**Check 4: Data directory writable**
```bash
touch data/test_write && rm data/test_write
```

**Check 5: Database connection**
```bash
# Verify the SQLite database is accessible
ls -la data/quad.db
```

### Symptom: `ImportError: No module named 'quad'`

```bash
# Reinstall the package in editable mode
pip install -e .
```

---

## Trading Issues

### Symptom: No Positions Opened

**Step 1: Check risk gate status**
```bash
quad risk
```
Look for any gate showing `FAIL`. Common rejections:

Quad runs the 9 futures pre-trade gates (see `src/quad/risk/gates.py`).
They are evaluated in order and **short-circuit on the first failure**, so
only the first blocking gate is reported. Common rejections:

| Gate | Rejection Example | Fix |
|---|---|---|
| Max Positions | "max positions 1 exceeded" | Raise `risk.max_positions` (serial-trade mode allows only 1) |
| Portfolio Risk | "portfolio risk would exceed ...%" | Reduce size, or raise `risk.max_portfolio_risk_pct` |
| Daily Loss | "daily loss limit ..." | Stop for the day; do not widen the limit mid-session |
| Drawdown | "drawdown ...% exceeds ..." | Same — the breaker is there for a reason |
| Liquidation Risk | "too close to liquidation" | Add margin (lower leverage) — the threshold is leverage-aware |
| Funding Cost | "funding cost ... exceeds budget" | Pick a symbol/timeframe with cheaper carry |
| Leverage Limit | "leverage ...x exceeds limit" | Lower `trading.leverage` (clamped to `risk.max_leverage` at startup) |
| Concentration | "concentration ...% exceeds ..." | Reduce size or spread across symbols |
| Correlation | "quote-asset group ...% of portfolio" | Reduce correlated exposure |

To see which gate is actually blocking, run `/risk` in Telegram (it reports
per-gate status) or inspect the `gate=` field on the `order_rejected_by_risk`
log event — it names the failing gate directly.

**Step 2: Check circuit breakers**
```bash
quad risk
# and, for live component state:
quad health
```
If any breaker is `ACTIVE`, it must be resolved before trading resumes. The
kill switch is reset with a one-shot token of the form `KILL_RESET_<uuid>`
(see `src/quad/risk/circuit_breakers.py`).

**Step 3: Check strategy**
```bash
quad strategies
quad config
```
Verify the strategy is enabled (`strategy.<name>.enabled: true`) and its
parameters are in range. `quad config` prints the *resolved* configuration
with secrets redacted.

### Symptom: Startup aborts with "futures account setup incomplete"

Startup deliberately **fails** in live mode when the exchange does not
confirm the leverage and margin mode that was requested. Otherwise the bot
would size brackets and liquidation distances from a number the exchange
never accepted.

Common causes: an open position on the symbol (Bybit refuses a leverage
change while a position is open), or the symbol's risk-limit tier. Close
the position or lower the leverage. The error names the failing symbols and
the requested-vs-actual leverage. In dry-run the same condition is logged as
`account_setup_leverage_mismatch` and startup continues.

### Symptom: Orders Not Executing

```bash
# Check exchange connectivity
quad health
# Look for "Exchange: connected" and "latency: < 500ms"

# Check open orders
quad orders --open
```

**Possible causes:**
- **Rate limited**: Bybit has strict rate limits. Check logs for `429` errors.
- **Invalid price**: Futures prices move fast. Your limit price may be too far from market.
- **Insufficient margin**: The exchange rejected the order. Check account balance.
- **Delisted symbol**: The symbol may have been delisted or renamed. Verify the symbol (e.g. `BTCUSDT`).
- **Post-only rejected**: If using post-only, the order may have been immediately fillable.

### Symptom: Orders Partially Filled

Futures orders can be partially filled due to low liquidity on smaller symbols. The bot tracks partial fills and will:

1. Log the partial fill with filled quantity
2. Leave the remaining order open
3. Attempt to fill the remainder on the next cycle
4. Cancel and replace if the remaining quantity has been open too long

**Manual intervention:**
```bash
# Check order status as last persisted by the trading cycle
quad orders
```

Order cancellation is **Telegram-only** — the CLI has no cancel command:

| Telegram command | Who | Effect |
|---|---|---|
| `/cancel <order_id>` | any bound chat | Cancel one order by id |
| `/kill` | operator only | Halt new entries and cancel **all** open orders, reporting cancelled/failed counts |

---

## WebSocket Issues

### Symptom: Frequent WebSocket Disconnections

**Check 1: Network stability**
```bash
# Ping Bybit testnet
ping api-testnet.bybit.com
# Look for packet loss or high latency
```

**Check 2: Connection count**
```bash
quad health
```
Look for a `market_data` / `websocket` component. Bybit also limits
concurrent connections, so a growing symbol list increases reconnection
pressure.

**Check 3: Logs for disconnection reasons**
```bash
quad logs --lines 200
# or, if logs go to stdout (the default outside a file log), filter directly:
docker compose logs quad | grep -i websocket
```
Logs are JSON by default (`QUAD_LOG_FORMAT=json`), so each line is one
event — useful fields: `event`, `level`, `correlation_id`, `reconnect_count`.

**Troubleshooting:**
- Reduce number of subscribed symbols
- Check firewall/proxy settings
- Verify your IP is allowed (if using IP-restricted API keys)
- The bot auto-reconnects with exponential backoff + jitter, capped by
  `market_data.backoff.max_seconds` (default 30s)

---

## Database Issues

### Symptom: `connection refused` or `could not connect to server` Errors

Quad uses SQLite for persistence. This error can occur if:
- The data directory is not writable
- The database file path is incorrect
- The SQLite file is corrupted
- The connection pool is exhausted (unlikely with default settings)

**Resolution:**
```bash
# Check the data directory exists and is writable
ls -la data/

# Verify the DSN is configured correctly
quad config persistence.dsn

# Test the database file exists
ls -la data/quad.db
```

### Symptom: Database Connection Timeout

If the bot logs `TimeoutError` or `ConnectionError`:

```bash
# Check if the data directory is writable
ls -la data/

# Increase the busy_timeout in config
# persistence.dsn: "data/quad.db"

# Restart the bot (it runs in the foreground; Ctrl+C, then start again)
quad start
# In Docker: docker compose restart quad
```

### Symptom: Disk Full

```bash
# Check disk usage
df -h data/

# Check database size
ls -lh data/quad.db

# Free space
# - Run VACUUM ANALYZE to reclaim space
# - Archive or drop old data
# - Reduce log retention
```

---

## Configuration Issues

### Symptom: Config Changes Not Taking Effect

**Check if the setting is hot-reloadable:**

| Hot-Reloadable | Restart Required |
|---|---|
| Risk parameters | Exchange API keys |
| Strategy parameters | Database path |
| Log level | Mode (testnet/live) |
| Stop-loss/take-profit | Health server port |

**Force reload:**
```bash
quad config reload
```

**Check current effective value:**
```bash
quad config risk.max_position_size
```

### Symptom: YAML Parsing Errors

```bash
# Validate YAML syntax
python -c "import yaml; yaml.safe_load(open('config/config.local.yaml'))"

# Common issues:
# - Tabs instead of spaces (YAML requires spaces)
# - Missing quotes around strings with special characters
# - Incorrect indentation (2 spaces per level)
```

---

## CLI Issues

### Symptom: Command Not Found

```bash
# Ensure the package is installed
pip install -e .

# Verify the CLI entry point
quad --version

# If still not found, check PATH
which quad
# or on Windows: where quad
```

### Symptom: `quad start` Hangs

The bot may hang during startup if:
- Exchange connection is slow
- Historical data download is in progress
- Previous database is being migrated

```bash
# Start with verbose logging (log level is an env var, not a CLI flag)
QUAD_LOG_LEVEL=DEBUG quad start
# Human-readable instead of JSON:
QUAD_LOG_FORMAT=console quad start
```

---

## Exchange Connectivity

### Symptom: Exchange Connection Errors

```bash
# Check API key validity (Bybit V5; testnet host by default)
curl -H "X-BAPI-API-KEY: ***" \
  "https://api-testnet.bybit.com/v5/account/wallet-balance?accountType=UNIFIED"

# Expected: 200 with account data
# 401: Invalid API key
# 403: IP not whitelisted
# 429: Rate limited
```

**API key issues:**
- Key was revoked or expired
- Key permissions changed (needs trading permission)
- IP restriction blocking the request
- Wrong network (testnet key on production or vice versa)

### Symptom: Rate Limited (HTTP 429)

Bybit has strict rate limits (V5 API). The bot monitors its weight usage:

```bash
# The bot will automatically back off when approaching limits
# Default: 10 requests/second, 1200 weight/minute

# If consistently rate limited:
# 1. Reduce number of tracked symbols
# 2. Increase trading cycle interval
# 3. Check for multiple bot instances
```

---

## Telegram Issues

### Symptom: Telegram Bot Not Responding

**Check 1: Bot token**
```bash
# Verify TELEGRAM_BOT_TOKEN is set in .env
grep TELEGRAM_BOT_TOKEN .env
```

**Check 2: Bot connectivity**
```bash
quad health
# Look for "Telegram: connected (polling active)"
```

**Check 3: Network/firewall**
- Ensure outbound HTTPS (port 443) to `api.telegram.org` is allowed
- Corporate firewalls or VPNs may block Telegram API traffic

### Symptom: Authentication Failed (Wrong Chat ID)

```bash
# Message @userinfobot on Telegram to get your chat ID
```

The bot only responds to whitelisted chat IDs configured in your deployment. Verify `TELEGRAM_NOTIFICATION_CHAT_ID` is set correctly.

### Symptom: Polling Conflicts

Only one instance of the bot can poll Telegram at a time. If you see:
```
TelegramError: Conflict: terminated by other getUpdates request
```

This means another instance is polling. Stop the other instance before starting a new one. With Docker, ensure only one container is running:
```bash
docker ps | grep quad
```

### Symptom: Bot Token Invalid

If you regenerated the token via @BotFather, update `.env` and restart:
```bash
# Generate a new token from @BotFather
# Update .env with the new token
# Restart Quad (foreground process: Ctrl+C, then start again)
quad start
```

If the bot was blocked or reported, create a new bot via @BotFather and update the token.

### Symptom: Bot Messages Not Sent

The bot logs and continues if it cannot send a message:
```bash
docker compose logs quad | grep -i "telegram\|send_message"
# or, with a file log:
quad logs --lines 200
```
Because send failures are non-fatal by design, they do not stop the trading
cycle — check `/ai_status` or the log stream rather than expecting an alert.

Common causes:
- User blocked the bot
- Bot was removed from a group
- Rate limited by Telegram API (rare in polling mode)
- Chat ID format is incorrect (must be numeric)

---

## Error Reference

| Error | Meaning | Action |
|---|---|---|
| `ExchangeConnectionError` | Can't reach Bybit API | Check network, API status |
| `InvalidApiKeyError` | API key rejected | Verify key in `.env` |
| `RateLimitError` | Hit API rate limits | Reduce request frequency |
| `OrderRejectedError` | Exchange rejected order | Check order parameters |
| `InsufficientMarginError` | Not enough margin | Deposit USDT or reduce risk |
| `ConnectionError` | Database connection failed | Check data directory and DSN config |
| `ConfigValidationError` | Invalid configuration | Run `quad config` to validate |
| `StrategyValidationError` | Strategy params invalid | Check strategy parameters |
| `CircuitBreakerTripped` | A circuit breaker is active | Check `quad risk` and resolve |
| `KillSwitchTriggered` | Emergency shutdown active | Investigate root cause, manual reset |

---

## Logs and Debugging

### Enable Debug Logging

```bash
# Per-session debug
QUAD_LOG_LEVEL=DEBUG quad start

# Persistent debug — set in .env / .env.local:
# QUAD_LOG_LEVEL=DEBUG
# QUAD_LOG_FORMAT=console   # human-readable instead of JSON
```

### Correlating a Single Cycle or Webhook Request

Every trading cycle binds a correlation id and every TradingView alert gets
its own, so concurrent activity stays separable:

```bash
docker compose logs quad | grep 'correlation_id":"cycle-'
docker compose logs quad | grep 'correlation_id":"tv-'
```

### Structured Log Queries

```bash
# All errors in last 24 hours
tail -n 10000 data/logs/quad.log | grep '"ERROR"'

# All decisions today
grep '"decision"' data/logs/quad.log

# Cycle time statistics
grep '"trading_cycle"' data/logs/quad.log | \
  grep -o '"cycle_time_ms":[0-9]*' | \
  cut -d: -f2 | sort -n | tail -5
```

### Common Log Patterns

| Log Pattern | Meaning |
|---|---|
| `"Connection lost to exchange, reconnecting..."` | WebSocket dropped (auto-reconnect) |
| `"Rate limit approaching: 85% of weight used"` | Approaching rate limits |
| `"Gate REJECTED: margin_sufficiency"` | Pre-trade check failed |
| `"Circuit breaker TRIPPED: pnl_drawdown"` | Drawdown exceeded threshold |
| `"Kill switch ACTIVATED"` | Emergency shutdown triggered |
| `"Order FILLED: symbol=..., qty=..."` | Successful fill |
| `"Config hot-reload applied"` | Live config change succeeded |
