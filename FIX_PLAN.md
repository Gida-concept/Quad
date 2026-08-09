# Quad Fix Plan - 2026-08-08

Thorough plan to fix the issues found in the 2026-08-08 full codebase scan (NOTE-040). No code changes in this file; it is the plan only.

## Verification Protocol
- Before/after every phase, run: `ruff check src setup.py`, `mypy src`, `py_compile` on all files, import smoke test, and fresh-run checks (`python -m quad --help` exits 0 without starting the bot; `python -m quad` with `data/` absent starts cleanly).
- Record before/after counts; merge only on a full green re-run.

## Phase 1 - High (Runtime Bugs, fix first)
1. Circuit-breaker alert crash - `orchestrator.py:2344` passes int `tier` (circuit_breakers.py:34) to `html.escape()` -> AttributeError exactly when a breaker trips.
   - Fix: `esc(str(tier))`; audit every other `esc(...)` call in the file for non-str args.
   - Verify: offline harness calling `_notify_circuit_breaker(name=..., tier=3, reason=...)` with a stub bot; no exception, HTML escaping intact.
2. Fresh-start crash - `data/` does not exist and `_SQLitePool` never creates parent dirs -> `sqlite3.OperationalError: unable to open database file` on clean clones.
   - Fix: in `persistence/database.py` `_SQLitePool`, before `aiosqlite.connect`, run `os.makedirs(dirname(abspath(db_path)), exist_ok=True)` for any path other than `:memory:`.
   - Verify: with `data/` removed, a throwaway `DatabaseManager.connect()` creates `data/quad.db`; idempotent for Docker.
3. `python -m quad --help` ignored - `__main__.py` never parses argv, so `--help` boots the whole bot.
   - Fix: parse `sys.argv[1:]` with argparse in `main()`: `--help/-h` (usage, exit 0), `--version`, `--config PATH` (overrides `QUAD_CONFIG_PATH` env). Never start the orchestrator for help/version.
   - Verify: `--help` exits 0 with usage and no bot logs; `--version` prints version; `--config` shows in startup log.
4. Fragile `quad.types.__init__` `__all__` - types/__init__.py:13-19 relies on CPython binding submodule names via star imports; `import quad.types.__init__` raises `NameError: name 'market' is not defined`; 10 mypy errors.
   - Fix: import submodules explicitly and compose `__all__` from `(domain, exchange, market, risk, strategy).__all__` (dedupe-safe, order-preserving).
   - Verify: `python -B -c "import quad.types.__init__"` passes; `len(quad.types.__all__) == 26`; mypy name-defined errors gone; ruff F403/F405 gone.
5. Optional `query.data` crash - `bot/commands.py:1637` calls `.replace` on Optional[str]; adjacent unsafe derefs at 1638, 1680, 1736.
   - Fix: guard `if query.data is None: return ConversationHandler.END`; replace `dict | None` indexed assignment/get/pop with `or {}` / None-guards.
   - Verify: mypy union-attr count in commands.py drops; no bare `.data.replace(` remains.
6. Missing `datetime` import - `market_data/engine.py:431-432` uses `datetime` annotations without import (F821), hidden by `from __future__ import annotations`.
   - Fix: `from datetime import datetime`.
   - Verify: ruff F821 cleared; `typing.get_type_hints(MarketDataEngine.get_candles)` resolves.

## Phase 2 - Medium (Bugs and Static Debt)
7. Unchecked Optional derefs - orchestrator.py:1175 (risk_manager before None check), 1787/1947/1974/2438 (execution_engine), 381/1084 (config_manager.get).
   - Fix: hoist `if self._risk_manager is None: return/continue` guards; same for `_execution_engine`; reorder 1175 check before the call; local cache after None-assert for config_manager.
   - Verify: mypy union-attr in this file drops to ~0.
8. Mypy sweep - 239 errors in 22 files (commands.py 108, groq.py 26, repositories.py 22, context.py 20, orchestrator.py 15, rest).
   - Add `[tool.mypy]` to pyproject.toml (python_version 3.10, warn_unused_ignores). Fix by category: union-attr guards, `funding_rates=None` -> default dict, repository row typing, config-dict `.get()` defaults.
   - Gate: `mypy src` -> 0 errors.
9. Ruff sweep - 190 findings.
   - Trivial first: F841 (5 unused vars), ISC004 (5 SQL strings -> parenthesize in persistence/models.py), F821 (item 6), SIM102/C408/PERF102/PIE810/PLW1508/PYI034/RUF012/RUF015 (9 one-liners).
   - S110 (17 silent except:pass): log at debug/warning where context exists; keep silent only in shutdown/idempotent paths with `# noqa: S110` + comment.
   - BLE001 (152 blind except): deliberate boundary resilience; keep with `# noqa: BLE001` + rationale where intentional, narrow exception types where cheap. Record decision in an ADR.
   - Gate: `ruff check` -> 0 (or 0 minus documented intentional ignores).
10. Tests - 0 today. Add `tests/` with pytest; regression tests for every Phase-1 fix plus an all-modules import test. CI runs ruff + mypy + pytest.
11. `quad backtest` stub - cli/app.py:292 validates strategy, instantiates engine, then only prints instructions.
    - Fix now: honest failure (clear message + `raise typer.Exit(code=1)`); removes dead `engine` var (F841).
    - Follow-up: implement `get_candles` in market_data/historical.py and wire `engine.run(...)` end-to-end.
12. binance.py:704/714 `params` redefinition (mypy no-redef); exchange/factory.py:74 bool env default (PLW1508).
    - Fix: rename second block to `cancel_params`; factory env default to empty string then coerce bool.
    - Verify: mypy no-redef and ruff PLW1508 cleared.

## Phase 3 - Low (Docs, Infra, Hygiene)
13. Stale "options trading bot" branding - pyproject.toml, Dockerfile, docker-compose.yml, quad/__init__.py, docstrings. Update to USD-M futures wording; fix keywords/classifiers.
14. `docs/` untracked (8 files) - remove `docs/` from .gitignore and `git add docs/`; flag for user sign-off (repo policy choice). CLAUDE.md/.claude/.pm-agent stay ignored.
15. Health server auth - monitoring/health.py:175 + config.yaml: read `QUAD_HEALTH_API_KEY` env then config; default `bind_address` to 127.0.0.1 when no key configured; keep 0.0.0.0 only with key set.
    - Verify: no key -> loopback; key set -> no header 403, correct header 200.
16. Docker healthcheck port + compose version - Dockerfile HEALTHCHECK to use `${QUAD_HEALTH_PORT:-9090}`; remove deprecated `version: "3.8"` from docker-compose.yml.
17. Python version mismatch (local 3.10 vs Docker/README 3.12+) - standardize on 3.12: document, run suite on 3.12, rebuild local venv; or pin Docker to 3.10 (recommend 3.12 to match classifiers).
18. requirements.txt pinning - pin aiosqlite and groq to tested versions; add typing-extensions (declared in pyproject.toml); refresh header date.
19. Formatting drift - 44/69 files unformatted. Run `ruff format` once after logic changes; add `ruff format --check` to CI; keep formatting commit separate.
20. Git hygiene - descriptive conventional commit messages going forward; optional commit-msg hook; no history rewrite.

## Sequencing
- Phase 1 (1-6) -> Phase 2 quick wins (12, 9-trivials) -> item 7 -> item 8 -> item 10 -> items 11, 9-remainder -> Phase 3 (13-19). Item 20 is process, applied continuously.

## Exit Criteria
- `ruff check` 0, `mypy src` 0, `pytest -q` green, `py_compile` all files, `python -m quad --help` exits 0, fresh run without `data/` starts cleanly, `git status` shows only intended changes.
