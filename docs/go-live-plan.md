# Quad Go?Live Readiness Plan

Created: 2026-08-15 ? Source: 2026-08-14 runtime log (`pasted-text.txt`) + `config/config.yaml` + orchestrator/execution code review.

## 1. Why the bot is ?not trading?

The 2026-08-14 log shows the bot is **healthy and running**, but it is **deliberately not opening new trades** for two compounding reasons:

1. **`_dry_run: true` in `config/config.yaml` (top-level key).**
   - Executed everywhere: `ExecutionEngine._is_dry_run`, `BinanceFuturesAdapter` dry-run guard, `orchestrator.cycle_status dry_run=true`.
   - `dry_run_guard_active: false` because the exchange is also `testnet: true`; a true dry-run+live guard is only armed when `_dry_run=true AND testnet=false`. Here the bot runs on Binance testnet in dry-run mode, so no real money is at risk, but **it also never places a real order** ? it is a simulation layer.
2. **A position is open on BTCUSDT (`positions: 1`) and the rotation is in ?manage open position? mode.**
   - `rotation_managing_open_position symbol=BTCUSDT` at `11:00:31` ? CASE A of `_run_ai_rotation`: with an open position, the bot scans **only that symbol** and refuses `ENTER`, holding until the TP/SL bracket closes it (`rotation_hold_until_tp_sl`).
   - A decision row was created (`row_created table=decisions id=84`) and `cycle_status` reported `positions: 1`, `ai_used: true` ? the AI cycle runs fine, it just never sees a flat account.

So the bot is NOT broken: **it is configured to dry-run and it is sitting on an open BTCUSDT position that the TP/SL bracket has not closed, so rotation never advances to open a fresh trade.**

## 2. Immediate fixes to actually start trading (testnet)

- [ ] **Flip `_dry_run` to `false`** in `config/config.yaml` so `ExecutionEngine`/adapter stop short-circuiting order placement.
- [ ] **Decide the open-position policy**: either
  - (a) let `close_positions_on_start: true` flatten BTCUSDT on next start and rotation opens fresh; or
  - (b) keep the position and wait for the bracket to close it.
  `close_positions_on_start` and `max_hold_seconds: 21600` are already implemented (ADR-95/96), so a clean restart is the fastest path.
- [ ] **Confirm testnet has usable balance** (`/fapi/v2/balance`) before expecting entries to fill; testnet faucets are often empty, which makes ENTER orders fail at the exchange.
- [ ] Re-run the bot and confirm `cycle_status` shows `positions: 0` then `rotation_opened_position` on an ENTER with a filled order.

## 3. What the log confirms works (already built)

- Health endpoint `200`, both scheduled jobs (`risk_alert`, `liquidation_warning`) run on time.
- Binance testnet connected, position mode read OK, account setup OK (no `account_setup_*_failed`).
- AI cycle produces decisions (`ai_used: true`, decision `id=84` written) ? no Groq 429 / validator veto in this window.
- Rotation guards behave as designed: open position ? manage only; TP/SL bracket is the close path.
- `liquidation_warning sent=true warnings=1` every 5 min is a **routine alert**, not an error.

## 4. Go?live hardening plan (make it professional)

### A. Trading-mode safety layer
- Replace boolean `_dry_run` with an explicit 3-state `mode: dry-run | paper/testnet | live` validated at startup, with a **single gate** (`ExecutionEngine.execute`, adapter `place_order`, and any new entry path).
- Add **live-launch confirmation**: require an env token (`QUAD_LIVE_CONFIRM=yes`) AND a `--live` flag to arm live mode; log a distinct `live_mode_armed` event.
- Add startup **account preflight**: balance ? min margin, margin mode, position mode, leverage, and available margin, blocking startup on failure.

### B. Order & bracket reliability
- Keep the Algo Order API path for all conditional brackets (ADR-089) and add reconciliation for `algoStatus` + `clientAlgoId` so a bracket that fails to attach at entry is detected and the position is closed defensively.
- Confirm each ENTER is atomic: market entry + bracket attach; if bracket attach fails, exit the position immediately (no naked exposure).
- Add order-fill confirmations (event-driven via WS rather than polling) and fill-price capture in the decisions table.

### C. Risk hardening
- Move leverage from `50x` to a conservative default (e.g. `5x?10x`) for live; keep `max_leverage` in sync with `trading.leverage`.
- Add a **max daily loss breaker** (e.g. ?5% equity) that stops all trading until manual reset, and persist breaker state.
- Add a **max drawdown / equity floor** that halts rotation; wire it into `_run_ai_rotation` CASE A/B so it stops opening new trades, not just orders.
- Add kill-switch: `/fapi/v1/account` balance check before every ENTER; if available margin < 2? position margin, refuse.
- Add **hard stop-loss cap** (e.g. 2?3% per trade regardless of AI bracket), enforced in `_compute_bracket_prices`.

### D. AI & strategy quality gates
- Raise `min_confidence_to_trade` from `0.0` to a real threshold (e.g. ?0.6) and enforce `gate_mode: veto` for live; keep `warn` in testnet.
- Track AI hit-rate metrics (ADR-086) and add a **disqualifier** that pauses trading on a symbol after N consecutive losses.
- Add a **paper-trading shadow period** (testnet + `_dry_run=false` + recorded virtual PnL) for ? N days before considering real money.

### E. Observability & ops
- Add persistent JSONL logging with structured rotation events (`rotation_opened_position`, `rotation_bracket_attached`, `rotation_position_closed`) and PnL per close.
- Add Telegram notifications on: position open/close, bracket attach failure, risk breaker trip, daily PnL, and any `live` mode change.
- Add a `/status` endpoint exposing mode, positions, open orders, algo brackets, risk-breaker state, and last cycle result (not just the current aggregated summary).
- Add a startup blocklist: refuse to start in live mode when any of: `dry_run=true`, `testnet=true`, unknown `mode`, or `close_positions_on_start` unset.

### F. Testing & acceptance
- Extend `tests/` with: dry-run guard (no order sent), live-mode confirm required, bracket-attach failure ? defensive exit, daily-loss breaker trips and halts, and rotation flat?ENTER flow (mock forced-close + ENTER).
- Verify: `venv\Scripts\python.exe -m py_compile` on changed files, full pytest suite, manual testnet run with `_dry_run=false`, then a small live ?paper-capped? trial before going fully live.

## 5. Phased rollout

- **Phase 0 (this week):** flip dry-run off on testnet, verify rotation opens/closes real testnet positions for a few days.
- **Phase 1:** implement sections 4A?4C (mode gate, live confirm, preflight, bracket-fill guard, risk breakers, leverage cap).
- **Phase 2:** implement 4D?4E (AI quality gates, shadow metrics, structured logs, Telegram alerts, richer /status).
- **Phase 3:** go-live checklist sign-off: testnet proven >2 weeks, low leverage, max-loss breaker armed, monitoring alerts reachable, manual kill-switch tested, then enable live mode.

## 6. Explicit defaults chosen (unless changed)
- Keep `testnet: true` until Phase 3; live only after Phase 1+2 complete.
- Keep rotation strategy (one position at a time) ? it is the safest default for a single-position bot.
- Keep `price_bracket_check` + `max_hold_seconds` guards enabled.
- Default live leverage 5x; per-position SL 2%, TP 5% (overridable).
- Default `min_confidence_to_trade: 0.6`, `gate_mode: veto` for live.
