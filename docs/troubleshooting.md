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
# Must be 3.12 or later
```

**Check 2: Dependencies installed**
```bash
pip list | grep quad
# Should show quad with version
```

**Check 3: Config directory**
```bash
ls -la config/config.default.yaml
# Must exist and be valid YAML
```

**Check 4: Data directory writable**
```bash
touch data/test_write && rm data/test_write
```

**Check 5: Database connection**
```bash
# Verify the PostgreSQL server is reachable
pg_isready
# If DATABASE_URL is set, verify credentials and connectivity
psql "$DATABASE_URL" -c "SELECT 1;"
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

| Gate | Rejection Example | Fix |
|---|---|---|
| Margin | "Insufficient margin" | Deposit more USDT or reduce position size |
| Position Size | "Exceeds max of 5 contracts" | Increase `risk.max_position_size` |
| Delta Exposure | "Portfolio delta would exceed 5.0" | Close offsetting positions |
| Theta Decay | "Theta would exceed -100 USDT/day" | Adjust strategy or reduce negative theta |
| Volatility | "IV too low for iron condor" | Switch strategy or wait for volatility |
| Concentration | "ETH expiry exposure exceeds 40%" | Diversify across expiries |

**Step 2: Check circuit breakers**
```bash
quad risk
```
If any breaker is `ACTIVE`, it must be resolved before trading resumes.

**Step 3: Check strategy**
```bash
quad strategies
quad config strategy
```
Verify the strategy is enabled and parameters are within valid ranges.

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
- **Invalid price**: Option prices change quickly. Your limit price may be too far from market.
- **Insufficient margin**: The exchange rejected the order. Check account balance.
- **Expired contract**: The symbol may have been delisted or renamed. Verify contract symbol.
- **Post-only rejected**: If using post-only, the order may have been immediately fillable.

### Symptom: Orders Partially Filled

Futures orders can be partially filled due to low liquidity on smaller symbols. The bot tracks partial fills and will:

1. Log the partial fill with filled quantity
2. Leave the remaining order open
3. Attempt to fill the remainder on the next cycle
4. Cancel and replace if the remaining quantity has been open too long

**Manual intervention:**
```bash
# Check order status
quad orders --open

# Cancel remaining and reassess
quad cancel <order-id>
```

---

## WebSocket Issues

### Symptom: Frequent WebSocket Disconnections

**Check 1: Network stability**
```bash
# Ping Bybit testnet
ping testnet.bybit.com
# Look for packet loss or high latency
```

**Check 2: Connection count**
```bash
quad health
# Check ws streams count -- Bybit limits concurrent connections
```

**Check 3: Logs for disconnection reasons**
```bash
quad logs --level WARN | grep -i websocket
```

**Troubleshooting:**
- Reduce number of subscribed symbols
- Check firewall/proxy settings
- Verify your IP is allowed (if using IP-restricted API keys)
- The bot auto-reconnects with exponential backoff (1s, 2s, 4s, ... up to 60s)

---

## Database Issues

### Symptom: `connection refused` or `could not connect to server` Errors

Quad connects to a PostgreSQL server via the configured DSN. This error can occur if:
- The PostgreSQL server is not running or not reachable
- The DSN credentials (host, port, user, password) are incorrect
- The database does not exist
- Network/firewall is blocking the connection
- The connection pool is exhausted (unlikely with default `max_size=5`)

**Resolution:**
```bash
# Check PostgreSQL server status
pg_isready

# Verify the DSN is configured correctly
quad config persistence.dsn

# Test the connection manually
psql "$DATABASE_URL" -c "SELECT 1;"

# Ensure the database exists
createdb "$DATABASE_URL"
```

### Symptom: Database Connection Timeout

If the bot logs `TimeoutError` or `ConnectionDoesNotExistError`:

```bash
# Check if the server is overwhelmed
psql "$DATABASE_URL" -c "SELECT count(*) FROM pg_stat_activity;"

# Increase the pool size or command_timeout in config
# persistence.busy_timeout: 10000  # ms

# Restart the bot
quad stop && quad start
```

### Symptom: Disk Full

```bash
# Check disk usage
df -h data/

# Check database size
psql "$DATABASE_URL" -c "SELECT pg_size_pretty(pg_database_size(current_database()));"

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
# Start with verbose logging
quad start --log-level DEBUG
```

---

## Exchange Connectivity

### Symptom: Exchange Connection Errors

```bash
# Check API key validity
curl -H "X-BAPI-APIKEY: $BYBIT_API_KEY" \
  "https://api-testnet.bybit.com/v5/account/info"

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
# Restart Quad
quad stop
quad start
```

If the bot was blocked or reported, create a new bot via @BotFather and update the token.

### Symptom: Bot Messages Not Sent

The bot silently logs errors if it cannot send a message:
```bash
quad logs --level ERROR | grep -i telegram
```

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
| `ConnectionError` | PostgreSQL connection failed | Check `pg_isready` and DSN config |
| `ConfigValidationError` | Invalid configuration | Run `quad config` to validate |
| `StrategyValidationError` | Strategy params invalid | Check strategy parameters |
| `CircuitBreakerTripped` | A circuit breaker is active | Check `quad risk` and resolve |
| `KillSwitchTriggered` | Emergency shutdown active | Investigate root cause, manual reset |

---

## Logs and Debugging

### Enable Debug Logging

```bash
# Per-session debug
quad start --log-level DEBUG

# Persistent debug
# Set in .env:
# QUAD_LOG_LEVEL=DEBUG
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
