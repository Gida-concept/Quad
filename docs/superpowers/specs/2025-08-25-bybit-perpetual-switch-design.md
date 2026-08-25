# Switch Trading Bot from Binance to Bybit (USDT Perpetual)

**Date:** 2025-08-25
**Status:** Design — pending user approval before implementation
**Classification:** Bounded (new exchange adapter + wiring). No new subsystem, no interface redesign.

---

## 1. Goal

Switch the trading bot from Binance USDT-M Futures to **Bybit USDT perpetual
futures** using Bybit's V5 unified API.

**Hard constraints (from user):**

1. **Bybit USDT perpetual only** — `category=linear`. The bot trades
   perpetual swaps, not Bybit's inverse (`.inverse`) or spot (`spot`) markets.
   Perpetual is selected by the `category=linear` parameter; there is no
   separate "futures vs perpetual" toggle to misconfigure.
2. **Only two environments: testnet and live.** The existing `mock` exchange
   mode must be removed entirely — no mock adapter, no `mock` factory branch,
   no `mock` config option, no `QUAD_MODE=mock`, no mock in docs. Testnet is
   the default safety environment; live is opt-in.
3. **Use the official `pybit` SDK** (Bybit's maintained Python client) rather
   than hand-rolling REST + WebSocket signing like the Binance adapter does.
   This keeps the adapter small and gives us correct V5 auth, rate limiting,
   and WebSocket subscription handling for free.
4. Keep the existing `ExchangeAdapter` ABC and the bot's strategy/risk logic
   unchanged. The only change at the strategy layer is the exchange plumbing
   and the two Binance-specific error checks that live outside the adapter.

---

## 2. Current State (verified in code)

| Concern | Location | Detail |
|---|---|---|
| Adapter ABC | `src/quad/exchange/base.py:32` | `ExchangeAdapter(ABC)`, 19 abstract methods + shared filter/normalization helpers (`base.py:261-466`) |
| Binance adapter | `src/quad/exchange/binance.py:115` | `BinanceFuturesAdapter`, implements the ABC over `fapi.binance.com` |
| Mock adapter | `src/quad/exchange/mock.py` | `MockAdapter` — **to be deleted** |
| Factory | `src/quad/exchange/factory.py:62-85` | `create_exchange()` only branches `binance` / `mock`; `ValueError` otherwise; default `binance` (`:57`) |
| Config schema | `src/quad/config/schema.py` | `ExchangeConfig.name` validator `allowed = {"binance","mock"}` (`:305`); `BinanceConfig` block (`:100-218`); default `name="binance"` (`:269`) |
| Config file | `config/config.yaml:2,21` | `_mode: "binance"`, `exchange.name: "binance"`, `testnet: true` |
| Env | `.env.example:6,16-18` | `QUAD_MODE=binance`, `BINANCE_API_KEY/SECRET`, `BINANCE_TESTNET=true` |
| Docker | `docker-compose.yml:34,43-45` | `QUAD_MODE=${QUAD_MODE:-binance}`, `BINANCE_API_KEY/SECRET/TESTNET` |
| Margin-mode error | `src/quad/orchestrator/orchestrator.py:3334` | `_is_margin_mode_already_set()` checks Binance `-4046` only |
| Ghost-order error | `src/quad/execution/gateway.py:424` | `_is_order_not_found()` checks Binance `-2013` only |
| Position mode | `src/quad/exchange/binance.py:1035-1067` | `set_position_mode` / `get_position_mode` (`one_way`/`hedge`) |
| Leverage | `src/quad/exchange/binance.py:999` | `set_leverage` |

The ABC is clean and exchange-neutral; the adapter is the only place that
encodes exchange-specific behavior. This makes the switch a "write one new
adapter + rewire plumbing" task.

### Bybit V5 specifics (confirmed against Bybit docs)
- **USDT perpetual = `category=linear`.** `POST /v5/order/create` with
  `category=linear`, `symbol=BTCUSDT`, `side`, `orderType`, `qty`,
  `positionIdx` (0 = one-way, 1/2 = hedge). TP/SL attach inline via
  `takeProfit` / `stopLoss` / `tpslMode`. Conditional/stop orders use the same
  endpoint with `triggerPrice`.
- **Base URLs:** live `https://api.bybit.com`, testnet
  `https://api-testnet.bybit.com`. Public WS: `wss://stream.bybit.com/v5/public/linear`.
- **Auth:** `X-BAPI-API-KEY`, `X-BAPI-TIMESTAMP`, `X-BAPI-SIGN` (HMAC-SHA256
  of `timestamp + api_key + recv_window + body`). `pybit` handles this.
- **Instrument info:** `GET /v5/market/instruments-info?category=linear`
  returns `lotSizeFilter` (minQty/step) and `priceFilter` (minPrice/tick),
  which map directly onto the existing `LOT_SIZE` / `PRICE_FILTER` /
  `MIN_NOTIONAL` normalization in `base.py:261-466`.
- **Symbol format** `BTCUSDT` is already Bybit-compatible — no translation
  needed. Current settings (one-way, isolated, 50x) all exist under
  `category=linear`; no strategy change required.

---

## 3. Approach

Recommended and only approach: **Add `BybitFuturesAdapter` behind the existing
ABC, then delete the mock mode everywhere and rewire config/factory/env to
Bybit-only (testnet default).**

Rationale: the ABC already decouples the bot from any single exchange, so
duplicating the Binance pattern for Bybit is the lowest-risk path. Using
`pybit` (vs hand-rolling) shrinks the new adapter to ~call translation + the
shared normalization helpers the ABC already provides. Removing mock is a
mechanical deletion once `BybitFuturesAdapter` is the single concrete adapter.

No alternative approaches considered worth pursuing (e.g., a generic
multi-exchange facade) — YAGNI for a two-exchange, one-target bot.

---

## 4. Design

### 4.1 New adapter — `src/quad/exchange/bybit.py`

`class BybitFuturesAdapter(ExchangeAdapter)` implementing all 19 abstract
methods. Internals:

- **Client:** a single `pybit.HTTP(testnet=...)` for REST and a
  `pybit.WebSocket` (or `pybit.WebSocketV2`) subscribed to the `linear`
  public/private topics. `pybit` manages V5 signing, recv-window, and
  auto-reconnect.
- **`CATEGORY = "linear"`** as a class-level constant — perpetual is enforced
  by construction, not by config. Every market call passes `category=self.CATEGORY`.
- **`is_testnet`** returns the resolved `testnet` flag so the orchestrator's
  dry-run guard keeps working (same behavior the Binance adapter provided at
  `binance.py:327`).
- **Order mapping:** build the `pybit` order dict from `OrderRequest`
  (`base.py`): `symbol`, `side`, `orderType`, `qty`, `price` (if limit),
  `timeInForce`, `positionIdx=0` (one-way), `takeProfit`/`stopLoss` if present.
- **Normalization reuse:** return the existing `Account`, `Position`,
  `Order`, `OrderResult`, `FundingRate` dataclasses produced by the ABC's
  shared helpers (`base.py:261-466`); the adapter only translates Bybit's raw
  JSON into those shapes (no re-implementing filters).
- **Margin/position mode:** `set_margin_mode` → Bybit `setMarginMode`
  (`marginMode=ISOLATED`/`REGULAR`); `set_position_mode`/`get_position_mode`
  → Bybit `positionMode` (0 = one-way / 1 = hedge). Map `"one_way"`→0,
  `"hedge"`→1.
- **Error handling:** raise the same `ExchangeOrderError` (or the ABC's
  existing error type) the orchestrator/gateway already catch, but **do not
  hard-code Bybit error codes into the orchestrator** — see 4.4.

### 4.2 Factory — `src/quad/exchange/factory.py`

- Delete `from quad.exchange.mock import MockAdapter` (`:15`).
- Replace the `binance` branch with a `bybit` branch that reads
  `BYBIT_API_KEY` / `BYBIT_API_SECRET` / `BYBIT_TESTNET` and constructs
  `BybitFuturesAdapter(testnet=..., ...)`.
- Delete the `if mode == "mock": return MockAdapter()` branch (`:81-82`).
- `ValueError` message: `Expected one of: bybit`.
- Default mode becomes `bybit` (`:57`).

### 4.3 Config schema — `src/quad/config/schema.py`

- Add `BybitConfig` (`:100`) mirroring `BinanceConfig` but with Bybit URLs:
  `base_url="https://api.bybit.com"`, `testnet_base_url="https://api-testnet.bybit.com"`,
  `ws_base_url="wss://stream.bybit.com/v5/public/linear"`,
  `ws_testnet_base_url` same host testnet path.
- `ExchangeConfig`: `name` default `"bybit"` (`:269`), `allowed = {"bybit"}`
  (`:305`), replace the `binance: BinanceConfig` field (`:288`) with
  `bybit: BybitConfig`.
- Delete `BinanceConfig` class (only if nothing else references it — verify
  first).

### 4.4 Generalize the two Binance-only error checks

These live **outside** the adapter and must become exchange-agnostic:

- `orchestrator.py:3334` `_is_margin_mode_already_set(exc)` — currently checks
  Binance `-4046`. Replace the string check with an adapter-provided method,
  e.g. add `abstract`/concrete `is_margin_mode_already_set(exc) -> bool` to the
  ABC defaulting to `False`, and have `BybitFuturesAdapter` return `True` for
  Bybit's equivalent ("110043" / "Margin mode is not modified"). The
  orchestrator calls `adapter.is_margin_mode_already_set(exc)`.
- `execution/gateway.py:424` `_is_order_not_found(exc)` — currently checks
  Binance `-2013`. Same treatment: add `is_order_not_found(exc) -> bool` to the
  ABC (default `False`); `BybitFuturesAdapter` returns `True` for Bybit's
  "20001" / "order not exists" / "Order does not exist". Gateway calls
  `adapter.is_order_not_found(exc)`.

This keeps exchange error semantics inside the adapter where they belong.

### 4.5 Files / env / docker

- `config/config.yaml`: `_mode: "bybit"`, `exchange.name: "bybit"`,
  `testnet: true` (default stays testnet).
- `.env.example`: replace `QUAD_MODE=binance`, `BINANCE_*` with
  `QUAD_MODE=bybit`, `BYBIT_API_KEY`, `BYBIT_API_SECRET`, `BYBIT_TESTNET=true`.
- `docker-compose.yml:34,43-45`: `QUAD_MODE=${QUAD_MODE:-bybit}`,
  `BYBIT_API_KEY/SECRET/TESTNET`.
- `requirements.txt` / `pyproject.toml`: add `pybit>=1.0` (pin a known-good
  version).

### 4.6 Deletions (mock removal)

- Delete `src/quad/exchange/mock.py`.
- Remove every reference: factory import/branch, schema `mock` allowance,
  any test fixtures that construct `MockAdapter` (search `MockAdapter`,
  `mock`, `QUAD_MODE=mock`). Decide per-test whether to rewrite against
  `BybitFuturesAdapter` testnet (preferred) or drop the test.
- Update docs that mention `mock` mode (`docs/configuration.md`,
  `docs/deployment.md`, README) to reflect bybit-only + testnet/live.

---

## 5. Testing

- **Unit:** `tests/test_bybit_adapter.py` — construct `BybitFuturesAdapter`
  with `testnet=True`, assert `is_testnet is True`, `CATEGORY == "linear"`,
  and that order/position dicts map correctly (using `pybit` request
  inspection or a recorded-response fixture; do not hit the network in CI).
- **Contract:** assert `BybitFuturesAdapter` implements all 19 ABC methods.
- **Normalization:** feed a sample `instruments-info` JSON through the adapter
  and assert the resulting `LOT_SIZE`/`PRICE_FILTER` shapes match what the
  execution engine expects.
- **Error mapping:** assert `is_margin_mode_already_set` / `is_order_not_found`
  behave correctly for Bybit codes and that the orchestrator/gateway now call
  the adapter methods.
- **Mock removal:** full `grep` for `MockAdapter`/`mock`/`QUAD_MODE=mock`
  returns zero hits outside intentional bybit references.
- **Smoke (manual, testnet):** run the bot against Bybit testnet with a tiny
  balance; confirm connect → set leverage/margin → place/cancel order →
  position read → disconnect, with no Binance references in logs.

---

## 6. Risks / Open Items

- **`pybit` version pin** — pick a version that's current at implementation
  time; verify `WebSocket` topic names for `linear` private order/wallet
  streams.
- **Bybit error code numbers** used in 4.4 must be confirmed against live
  testnet responses during the smoke test (codes above are from Bybit docs
  and may need adjustment).
- **CI without secrets** — adapter tests must not require real API keys; use
  recorded fixtures / `pybit`'s `no_init`/mock-transport where available.
- **Leverage cap** — Bybit per-symbol max leverage may differ from Binance's
  50x; the adapter should surface the exchange-reported cap and let the
  existing risk config clamp.

---

## 7. Out of Scope

- **Binance adapter removal from disk:** out of scope. `binance.py` is left in
  place (unused) so a revert is trivial; it is simply no longer wired into the
  factory, schema, env, or docs. If a clean delete is wanted later, that is a
  separate, explicitly-requested task.
- Inverse/spot Bybit markets, options, unified margin beyond isolated
  perpetual.
- Strategy, risk, or breaker logic changes.
