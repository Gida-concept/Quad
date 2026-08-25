# Task: Switch Trading Bot from Binance to Bybit (USDT Perpetual)

**Spec:** `docs/superpowers/specs/2025-08-25-bybit-perpetual-switch-design.md`
**Status:** Planning

## Approach Summary

Add a new `BybitFuturesAdapter` implementing the existing `ExchangeAdapter` ABC
using the official `pybit` SDK, targeting Bybit USDT perpetual via
`category="linear"` (hard-coded). Remove the `mock` mode everywhere (factory,
config schema, env, docker, docs, tests). Rewire factory/config/env to Bybit
only with **testnet as the default**. Generalize the two Binance-only error
checks into adapter-provided methods so exchange error semantics stay inside the
adapter. The Binance adapter file is left on disk (unused) for easy revert.

## Affected Files

### Create
- `src/quad/exchange/bybit.py` — new `BybitFuturesAdapter` (19 ABC methods + `is_testnet`, `CATEGORY="linear"`, error helpers).
- `tests/test_bybit_adapter.py` — unit + contract + normalization + error-mapping tests (no network in CI).

### Modify
- `src/quad/exchange/base.py` — add `is_margin_mode_already_set(exc)->bool` and `is_order_not_found(exc)->bool` (default `False`) to the ABC; ensure shared normalization helpers (`base.py:261-466`) reused.
- `src/quad/exchange/factory.py` — drop `mock` import/branch, add `bybit` branch reading `BYBIT_API_KEY/SECRET/TESTNET`, default `bybit`, `ValueError` "Expected one of: bybit".
- `src/quad/config/schema.py` — add `BybitConfig` (Bybit URLs), set `ExchangeConfig.name` default `"bybit"` + `allowed={"bybit"}`, replace `binance:` field with `bybit:`. Verify `BinanceConfig` has no other references; delete if safe.
- `src/quad/orchestrator/orchestrator.py` — replace `_is_margin_mode_already_set(exc)` (`:3334`) call with `adapter.is_margin_mode_already_set(exc)`.
- `src/quad/execution/gateway.py` — replace `_is_order_not_found(exc)` (`:424`) call with `adapter.is_order_not_found(exc)`.
- `config/config.yaml` — `_mode:"bybit"`, `exchange.name:"bybit"`, `testnet:true`.
- `.env.example` — `QUAD_MODE=bybit`, `BYBIT_API_KEY/SECRET`, `BYBIT_TESTNET=true`.
- `docker-compose.yml` — `QUAD_MODE=${QUAD_MODE:-bybit}`, `BYBIT_API_KEY/SECRET/TESTNET`.
- `requirements.txt` / `pyproject.toml` — add `pybit` (pin a current version).
- `docs/configuration.md`, `docs/deployment.md`, README — drop `mock` mode, document bybit testnet/live.

### Delete
- `src/quad/exchange/mock.py` — remove `MockAdapter`.
- Any test/code that constructs `MockAdapter` or sets `QUAD_MODE=mock` (rewrite against `BybitFuturesAdapter` testnet or drop).

## Implementation Order

1. **Adapter ABC extensions** (`base.py`): add `is_margin_mode_already_set` / `is_order_not_found` with `False` defaults. No behavior change yet.
2. **New adapter** (`bybit.py`): implement all 19 ABC methods over `pybit`. `CATEGORY="linear"`, `is_testnet` from flag, order/position dict mapping, margin/position-mode mapping, error helpers returning Bybit codes. Reuse ABC normalization helpers.
3. **Factory** (`factory.py`): `bybit` branch + default, drop `mock`.
4. **Config schema** (`schema.py`): `BybitConfig`, `name` default/allowed, swap field; delete `BinanceConfig` if unreferenced.
5. **Error-check rewiring** (`orchestrator.py:3334`, `gateway.py:424`): call adapter methods.
6. **Env/config/docker** (`config.yaml`, `.env.example`, `docker-compose.yml`): bybit + testnet default.
7. **Dependencies** (`requirements.txt`/`pyproject.toml`): pin `pybit`.
8. **Mock removal** (`mock.py` delete + grep sweep for `MockAdapter`/`mock`/`QUAD_MODE=mock`; fix tests).
9. **Docs** (`configuration.md`, `deployment.md`, README): bybit-only, testnet/live.
10. **Tests** (`tests/test_bybit_adapter.py`): contract, normalization, error mapping, no-network fixtures.

## Testing Strategy

- Unit: construct `BybitFuturesAdapter(testnet=True)` → assert `is_testnet is True`, `CATEGORY=="linear"`, order/position dicts correct, all 19 methods present (contract).
- Normalization: feed sample `instruments-info` JSON → assert `LOT_SIZE`/`PRICE_FILTER` shapes match execution engine expectations.
- Error mapping: assert adapter helpers true for Bybit codes; orchestrator/gateway call adapter methods (not string literals).
- Mock removal: `grep` for `MockAdapter`/`QUAD_MODE=mock` returns zero hits outside intentional bybit refs.
- Smoke (manual, testnet): run bot vs Bybit testnet with tiny balance — connect → set leverage/margin → place/cancel → position read → disconnect; no Binance references in logs.
- CI must not require real API keys (recorded fixtures / `pybit` mock transport).

## Risk Assessment

- **pybit version pin** — pick a current version; verify `WebSocket` topic names for `linear` private streams.
- **Bybit error codes** in step 4 may need adjustment vs live testnet (codes from docs: margin "110043", order-not-found "20001") — confirm during smoke test.
- **CI secrets** — adapter tests must avoid network; use fixtures.
- **Leverage cap** — Bybit per-symbol max may differ from 50x; adapter surfaces exchange cap, risk config clamps.
- **Revert safety** — `binance.py` left on disk (unwired) so a revert is trivial.

## Approval

Plan status: **APPROVED** — implementation may proceed.
