# 7-Day Strategy Self-Optimization Cycle

> **Terminology note:** "Auto-retrain" is used loosely here. The GroqClient is pure inference
> (llama-3.3-70b-versatile) with no fine-tuning API — there is no model retraining. What this
> feature actually does is **strategy self-optimization**: analyzing past trading decisions vs.
> outcomes and adjusting strategy parameters, risk thresholds, and AI prompts based on
> performance data.

---

## 1. Overview

Every 7 days, a scheduled job runs a self-optimization cycle:

1. **Data Collection** — Pull decisions, trades, and performance snapshots from the DB.
2. **Performance Analysis** — Feed a structured summary to Groq (with a dedicated analysis
   prompt) and receive JSON recommendations for parameter/prompt changes.
3. **Recommendation Generation** — Parse and validate the LLM output into a list of typed
   optimization recommendations.
4. **Change Application** — Apply high-confidence recommendations to the active config and
   strategy params (if `auto_apply` is enabled), then log all changes.

The cycle respects a circuit breaker: if the number of consecutive failed cycles exceeds 3,
the optimizer pauses until manually reset.

---

## 2. Motivation

| Pain point | How optimization helps |
|---|---|
| Static AI prompt never adapts to market regime changes | System prompt can be updated with recent performance insights |
| Strategy parameters (IV threshold, profit target %) stay manually tuned | Numeric parameters can be adjusted based on win/loss data |
| Risk checks are hard-coded | Risk thresholds can be tightened/loosened based on drawdown history |
| No feedback loop between outcomes and future decisions | The analysis closes the loop: "we lost on iron condors with IV < 30, avoid that" |

---

## 3. Design

### 3.1 Configuration — `RetrainConfig`

Add a new Pydantic v2 sub-model to `src/quad/config/schema.py`.

```python
class RetrainConfig(BaseModel):
    """Configuration for the 7-day strategy self-optimization cycle."""

    enabled: bool = Field(
        default=False,
        description="Enable / disable the optimisation cycle",
    )
    interval_days: int = Field(
        default=7,
        ge=1,
        le=90,
        description="Days between optimisation runs",
    )
    min_trades_for_analysis: int = Field(
        default=10,
        ge=1,
        description="Minimum trades in the period to produce recommendations",
    )
    max_history_days: int = Field(
        default=90,
        ge=1,
        le=365,
        description="How far back (days) to pull data for analysis",
    )
    confidence_threshold: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Minimum confidence score to auto-apply a recommendation",
    )
    auto_apply: bool = Field(
        default=False,
        description="Apply high-confidence recommendations without manual approval",
    )
    max_recommendations_per_cycle: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Cap on recommendations per cycle",
    )
    groq_temperature: float = Field(
        default=0.2,
        ge=0.0,
        le=1.0,
        description="Temperature for optimisation analysis calls",
    )
    groq_max_tokens: int = Field(
        default=2048,
        ge=512,
        le=8192,
        description="Max tokens for optimisation analysis calls",
    )
    analysis_prompt_override: str | None = Field(
        default=None,
        description="Override the default optimisation system prompt",
    )
```

Also add the field to `QuadConfig` (around line 786):

```python
retrain: RetrainConfig = Field(
    default_factory=RetrainConfig,
    description="7-day strategy self-optimisation cycle config",
)
```

### 3.2 Database Schema — new models

Add two new dataclass models to `src/quad/persistence/models.py`, following the existing
pattern (NOT SQLAlchemy ORM — plain `@dataclass` with `__tablename__`, `create_table_ddl()`,
`to_row()`, `from_row()`, `columns()`).

#### `OptimizationRunModel`

Represents one execution of the 7-day cycle.

```python
@dataclass
class OptimizationRunModel:
    """One execution of the self-optimization cycle."""

    __tablename__: ClassVar[str] = "optimization_runs"

    id: int
    run_at: int
    trigger: str                # "scheduled" | "manual" | "startup"
    decisions_analyzed: int
    trades_analyzed: int
    recommendations_count: int
    applied_count: int
    status: str                 # "running" | "completed" | "failed" | "skipped"
    started_at: int
    completed_at: int | None
    summary_json: str           # JSON: brief LLM summary + aggregated metrics
    error_message: str

    @classmethod
    def create_table_ddl(cls) -> str: ...
    @classmethod
    def columns(cls) -> list[str]: ...
    def to_row(self) -> tuple: ...
    @classmethod
    def from_row(cls, row: tuple) -> Self: ...
```

**CREATE TABLE DDL:**

```sql
CREATE TABLE IF NOT EXISTS optimization_runs (
    id SERIAL PRIMARY KEY,
    run_at BIGINT NOT NULL,
    trigger TEXT NOT NULL DEFAULT 'scheduled',
    decisions_analyzed INTEGER NOT NULL DEFAULT 0,
    trades_analyzed INTEGER NOT NULL DEFAULT 0,
    recommendations_count INTEGER NOT NULL DEFAULT 0,
    applied_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    started_at BIGINT NOT NULL,
    completed_at BIGINT,
    summary_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT NOT NULL DEFAULT ''
)
```

#### `OptimizationRecommendationModel`

One atomic recommendation produced by a run.

```python
@dataclass
class OptimizationRecommendationModel:
    """A single recommendation from an optimization run."""

    __tablename__: ClassVar[str] = "optimization_recommendations"

    id: int
    run_id: int                      # FK -> optimization_runs.id
    recommendation_type: str         # "parameter_adjustment" | "prompt_update" | "risk_threshold" | "strategy_toggle"
    target_area: str                 # e.g. "iron_condor.max_iv", "system_prompt", "circuit_breaker.max_daily_loss"
    current_value: str               # Before value (JSON string)
    recommended_value: str           # After value (JSON string)
    rationale: str                   # LLM explanation
    impact_estimate: str             # e.g. "+2.3% win rate", "medium"
    confidence: str                  # "low" | "medium" | "high"
    status: str                      # "pending" | "applied" | "rejected" | "failed"
    applied_at: int | None
    applied_strategy_params_json: str  # Snapshot of strategy params at apply time

    @classmethod
    def create_table_ddl(cls) -> str: ...
    @classmethod
    def columns(cls) -> list[str]: ...
    def to_row(self) -> tuple: ...
    @classmethod
    def from_row(cls, row: tuple) -> Self: ...
```

**CREATE TABLE DDL:**

```sql
CREATE TABLE IF NOT EXISTS optimization_recommendations (
    id SERIAL PRIMARY KEY,
    run_id INTEGER NOT NULL,
    recommendation_type TEXT NOT NULL,
    target_area TEXT NOT NULL,
    current_value TEXT NOT NULL DEFAULT '',
    recommended_value TEXT NOT NULL DEFAULT '',
    rationale TEXT NOT NULL DEFAULT '',
    impact_estimate TEXT NOT NULL DEFAULT '',
    confidence TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'pending',
    applied_at BIGINT,
    applied_strategy_params_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (run_id) REFERENCES optimization_runs(id)
)
```

#### Index definitions

Add to the `INDEX_DEFINITIONS` list in `models.py`:

```python
"CREATE INDEX IF NOT EXISTS idx_opt_runs_status ON optimization_runs(status)",
"CREATE INDEX IF NOT EXISTS idx_opt_runs_run_at ON optimization_runs(run_at)",
"CREATE INDEX IF NOT EXISTS idx_opt_recommendations_run_id ON optimization_recommendations(run_id)",
"CREATE INDEX IF NOT EXISTS idx_opt_recommendations_type ON optimization_recommendations(recommendation_type)",
"CREATE INDEX IF NOT EXISTS idx_opt_recommendations_status ON optimization_recommendations(status)",
```

#### Registry

Add both models to the `ALL_MODELS` list (between `ErrorLogModel` and the closing bracket):

```python
ALL_MODELS: list[type] = [
    # ... existing 12 models ...
    OptimizationRunModel,
    OptimizationRecommendationModel,
]
```

Bump `SCHEMA_VERSION` to `2` and add a migration entry.

### 3.3 Repositories

Add two new repository classes to `src/quad/persistence/repositories.py`, following
`BaseRepository[T]` pattern.

#### `OptimizationRunRepository`

```python
class OptimizationRunRepository(BaseRepository[OptimizationRunModel]):
    """Repository for optimization_run operations."""

    def __init__(self, pool) -> None:
        super().__init__(pool, OptimizationRunModel)

    async def get_by_date_range(
        self, start: int, end: int
    ) -> list[OptimizationRunModel]:
        """Return runs within a timestamp range."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM optimization_runs "
                "WHERE run_at >= $1 AND run_at <= $2 "
                "ORDER BY run_at DESC",
                start, end,
            )
            return [self._model.from_row(row) for row in rows]

    async def get_recent(self, limit: int = 10) -> list[OptimizationRunModel]:
        """Return the most recent runs."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM optimization_runs "
                "ORDER BY run_at DESC LIMIT $1",
                limit,
            )
            return [self._model.from_row(row) for row in rows]

    async def get_by_status(self, status: str) -> list[OptimizationRunModel]:
        """Return runs with a given status."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM optimization_runs WHERE status = $1 "
                "ORDER BY run_at DESC",
                status,
            )
            return [self._model.from_row(row) for row in rows]

    async def get_latest(self) -> OptimizationRunModel | None:
        """Return the most recent run (any status)."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM optimization_runs ORDER BY run_at DESC LIMIT 1"
            )
            return self._model.from_row(row) if row else None
```

#### `OptimizationRecommendationRepository`

```python
class OptimizationRecommendationRepository(BaseRepository[OptimizationRecommendationModel]):
    """Repository for optimization_recommendation operations."""

    def __init__(self, pool) -> None:
        super().__init__(pool, OptimizationRecommendationModel)

    async def get_by_run(self, run_id: int) -> list[OptimizationRecommendationModel]:
        """Return all recommendations for a given run."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM optimization_recommendations "
                "WHERE run_id = $1 ORDER BY id",
                run_id,
            )
            return [self._model.from_row(row) for row in rows]

    async def get_pending(self) -> list[OptimizationRecommendationModel]:
        """Return all recommendations with status = 'pending'."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM optimization_recommendations "
                "WHERE status = 'pending' ORDER BY run_id DESC, id"
            )
            return [self._model.from_row(row) for row in rows]

    async def get_by_type(self, recommendation_type: str) -> list[OptimizationRecommendationModel]:
        """Return recommendations of a given type."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM optimization_recommendations "
                "WHERE recommendation_type = $1 ORDER BY id",
                recommendation_type,
            )
            return [self._model.from_row(row) for row in rows]

    async def get_by_status(self, status: str) -> list[OptimizationRecommendationModel]:
        """Return recommendations with a given status."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM optimization_recommendations "
                "WHERE status = $1 ORDER BY run_id DESC",
                status,
            )
            return [self._model.from_row(row) for row in rows]

    async def mark_applied(self, recommendation_id: int, applied_at: int,
                           strategy_params_json: str) -> None:
        """Mark a recommendation as applied."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE optimization_recommendations "
                "SET status = 'applied', applied_at = $1, "
                "    applied_strategy_params_json = $2 "
                "WHERE id = $3",
                applied_at, strategy_params_json, recommendation_id,
            )
```

### 3.4 Analysis Engine — `ai/optimizer.py` (NEW)

New file at `src/quad/ai/optimizer.py`. The engine orchestrates the 4-phase cycle.

```
src/quad/ai/optimizer.py
```

```python
"""Strategy self-optimization engine.

Orchestrates the 4-phase cycle:
1. Data Collection  — fetch decisions, trades, performance from DB
2. Performance Analysis — feed structured data to Groq for analysis
3. Recommendation Generation — parse LLM output into typed recommendations
4. Change Application — persist recommendations, optionally apply high-confidence ones
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any

import structlog

from quad.config.schema import QuadConfig
from quad.ai.groq import GroqClient
from quad.ai.prompt import build_optimization_prompt
from quad.persistence.models import (
    OptimizationRunModel,
    OptimizationRecommendationModel,
)
from quad.persistence.repositories import (
    DecisionRepository,
    TradeRepository,
    PerformanceSnapshotRepository,
    OptimizationRunRepository,
    OptimizationRecommendationRepository,
)

logger = structlog.get_logger(__name__)

_MAX_CONSECUTIVE_FAILURES = 3


class Optimizer:
    """Self-optimization engine for strategy parameters and AI prompts.

    Parameters
    ----------
    config:
        Full application config (reads ``config.retrain`` for settings).
    groq_client:
        GroqClient instance used for inference-based analysis.
    decision_repo:
        Repository for decision records.
    trade_repo:
        Repository for trade records.
    performance_repo:
        Repository for performance snapshots.
    run_repo:
        Repository for optimization runs.
    recommendation_repo:
        Repository for optimization recommendations.
    """

    def __init__(
        self,
        config: QuadConfig,
        groq_client: GroqClient,
        decision_repo: DecisionRepository,
        trade_repo: TradeRepository,
        performance_repo: PerformanceSnapshotRepository,
        run_repo: OptimizationRunRepository,
        recommendation_repo: OptimizationRecommendationRepository,
    ) -> None:
        self._config = config
        self._retrain_cfg = config.retrain
        self._groq = groq_client
        self._decision_repo = decision_repo
        self._trade_repo = trade_repo
        self._perf_repo = performance_repo
        self._run_repo = run_repo
        self._rec_repo = recommendation_repo
        self._log = logger.bind()
        self._consecutive_failures = 0

    async def run_cycle(self, trigger: str = "scheduled") -> OptimizationRunModel:
        """Execute one full optimisation cycle.

        Parameters
        ----------
        trigger:
            What triggered this run (``"scheduled"`` | ``"manual"`` | ``"startup"``).

        Returns
        -------
        OptimizationRunModel
            The persisted run record with final status.
        """
        run = OptimizationRunModel(
            id=0,
            run_at=int(time.time() * 1000),
            trigger=trigger,
            decisions_analyzed=0,
            trades_analyzed=0,
            recommendations_count=0,
            applied_count=0,
            status="running",
            started_at=int(time.time() * 1000),
            completed_at=None,
            summary_json="{}",
            error_message="",
        )
        run = await self._run_repo.create(run)
        self._log = self._log.bind(run_id=run.id)

        try:
            # ---- Phase 1: Data Collection ----
            now_ms = int(time.time() * 1000)
            lookback_ms = self._retrain_cfg.max_history_days * 86400 * 1000
            since = now_ms - lookback_ms

            decisions = await self._decision_repo.get_by_date_range(since, now_ms)
            trades = await self._trade_repo.get_by_date_range(since, now_ms)
            perf_records = await self._perf_repo.get_by_date_range(since, now_ms)

            if len(trades) < self._retrain_cfg.min_trades_for_analysis:
                run.status = "skipped"
                run.summary_json = json.dumps({
                    "reason": f"Only {len(trades)} trades found; minimum is "
                              f"{self._retrain_cfg.min_trades_for_analysis}",
                })
                run.completed_at = int(time.time() * 1000)
                await self._run_repo.update(run.id, status=run.status,
                                            summary_json=run.summary_json,
                                            completed_at=run.completed_at)
                self._log.info("optimization_skipped", trades=len(trades))
                return run

            run.decisions_analyzed = len(decisions)
            run.trades_analyzed = len(trades)

            # ---- Phase 2: Performance Analysis ----
            analysis = await self._request_analysis(decisions, trades, perf_records)

            # ---- Phase 3: Recommendation Generation ----
            recommendations = self._parse_recommendations(analysis, run.id)
            run.recommendations_count = len(recommendations)

            for rec in recommendations:
                await self._rec_repo.create(rec)

            # ---- Phase 4: Change Application ----
            applied = 0
            if self._retrain_cfg.auto_apply:
                for rec in recommendations:
                    if self._should_apply(rec):
                        try:
                            await self._apply_recommendation(rec)
                            rec.status = "applied"
                            rec.applied_at = int(time.time() * 1000)
                            applied += 1
                        except Exception as exc:
                            self._log.warning("optimization_apply_failed",
                                              rec_id=rec.id, error=str(exc))
                            rec.status = "failed"

            run.applied_count = applied
            run.status = "completed"
            run.completed_at = int(time.time() * 1000)
            run.summary_json = json.dumps(analysis.get("summary", {}))
            self._consecutive_failures = 0

        except Exception as exc:
            self._consecutive_failures += 1
            run.status = "failed"
            run.completed_at = int(time.time() * 1000)
            run.error_message = str(exc)[:500]
            self._log.error("optimization_failed",
                            error=str(exc),
                            consecutive=self._consecutive_failures)

        finally:
            await self._run_repo.update(
                run.id,
                status=run.status,
                decisions_analyzed=run.decisions_analyzed,
                trades_analyzed=run.trades_analyzed,
                recommendations_count=run.recommendations_count,
                applied_count=run.applied_count,
                completed_at=run.completed_at,
                summary_json=run.summary_json,
                error_message=run.error_message,
            )
            self._log = logger.bind()

        return run

    async def _request_analysis(
        self,
        decisions: list,
        trades: list,
        perf_records: list,
    ) -> dict[str, Any]:
        """Send structured performance data to Groq and return the analysis."""
        prompt = build_optimization_prompt(
            decisions=decisions,
            trades=trades,
            performance=perf_records,
            current_config=self._config,
        )
        raw = await self._groq.chat(
            system=prompt["system"],
            user=prompt["user"],
            temperature=self._retrain_cfg.groq_temperature,
            max_tokens=self._retrain_cfg.groq_max_tokens,
        )
        # Parse JSON from the response (handles markdown code fences)
        text = raw.strip()
        if text.startswith("```"):
            first = text.find("{")
            last = text.rfind("}")
            if first != -1 and last != -1:
                text = text[first : last + 1]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            self._log.warning("optimization_parse_failed", preview=raw[:300])
            return {"summary": {}, "recommendations": []}

    def _parse_recommendations(
        self,
        analysis: dict[str, Any],
        run_id: int,
    ) -> list[OptimizationRecommendationModel]:
        """Convert LLM analysis output into recommendation model instances."""
        results: list[OptimizationRecommendationModel] = []
        raw_recs = analysis.get("recommendations", [])
        max_recs = self._retrain_cfg.max_recommendations_per_cycle

        for item in raw_recs[:max_recs]:
            results.append(OptimizationRecommendationModel(
                id=0,
                run_id=run_id,
                recommendation_type=item.get("type", "parameter_adjustment"),
                target_area=item.get("target_area", ""),
                current_value=json.dumps(item.get("current_value", "")),
                recommended_value=json.dumps(item.get("recommended_value", "")),
                rationale=item.get("rationale", ""),
                impact_estimate=item.get("impact_estimate", ""),
                confidence=item.get("confidence", "medium"),
                status="pending",
                applied_at=None,
                applied_strategy_params_json="{}",
            ))
        return results

    def _should_apply(self, rec: OptimizationRecommendationModel) -> bool:
        """Decide whether a recommendation meets the confidence threshold."""
        confidence_map = {"low": 0.3, "medium": 0.6, "high": 0.9}
        score = confidence_map.get(rec.confidence, 0.0)
        return score >= self._retrain_cfg.confidence_threshold

    async def _apply_recommendation(self, rec: OptimizationRecommendationModel) -> None:
        """Apply a single recommendation to the running config.

        This is a placeholder — the actual implementation depends on how strategy
        params and prompts are managed at runtime. Possibilities:

        - Update in-memory strategy params dict
        - Update system_prompt_override in AiConfig
        - Write a ConfigChangeModel audit log entry
        - Persist to strategy_state table
        """
        # TODO: Implement actual parameter/prompt application
        # For now, log the intended change
        self._log.info(
            "optimization_applied",
            rec_id=rec.id,
            target=rec.target_area,
            to=rec.recommended_value,
        )

    @property
    def is_paused(self) -> bool:
        """Check if the optimizer has exceeded the max consecutive failure threshold."""
        return self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES

    def reset_failure_count(self) -> None:
        """Manually reset the consecutive failure counter."""
        self._consecutive_failures = 0
```

### 3.5 Prompts — `ai/prompt.py` additions

Add to `src/quad/ai/prompt.py`:

```python
OPTIMIZATION_SYSTEM_PROMPT: str = """You are a trading strategy optimization analyst. Your job is to review recent trading performance and recommend concrete, actionable improvements.

You will receive:
1. A summary of recent trading decisions (actions taken, reasoning provided)
2. A summary of executed trades (PnL, fees, fill quality)
3. Performance metrics (portfolio value trend, drawdown, win rate)
4. Current strategy configuration (filters, thresholds, risk limits)

Your output must be valid JSON with this exact structure:
{
  "summary": {
    "period_win_rate": <number>,
    "period_pnl": <string>,
    "max_drawdown": <string>,
    "key_observation": <string>
  },
  "recommendations": [
    {
      "type": "parameter_adjustment" | "prompt_update" | "risk_threshold" | "strategy_toggle",
      "target_area": <string>,
      "current_value": <any>,
      "recommended_value": <any>,
      "rationale": <string>,
      "impact_estimate": <string>,
      "confidence": "low" | "medium" | "high"
    }
  ]
}

Focus recommendations on:
- Strategy parameters that correlate with poor outcomes (e.g., IV too low, profit target too tight)
- Risk threshold adjustments based on drawdown history
- Prompt improvements based on recurring reasoning errors
- Strategy toggling if a strategy consistently underperforms in current conditions
"""

def build_optimization_prompt(
    decisions: list,
    trades: list,
    performance: list,
    current_config: Any,
) -> dict[str, str]:
    """Build the system and user prompts for the optimization analysis cycle.

    Parameters
    ----------
    decisions:
        List of DecisionModel instances from the lookback period.
    trades:
        List of TradeModel instances from the lookback period.
    performance:
        List of PerformanceSnapshotModel instances from the lookback period.
    current_config:
        The current QuadConfig instance.

    Returns
    -------
    dict
        With keys ``system`` and ``user``.
    """
    system = (
        current_config.ai.system_prompt_override
        or OPTIMIZATION_SYSTEM_PROMPT
    )

    # Build compact summaries
    total_decisions = len(decisions)
    executed_decisions = sum(1 for d in decisions if getattr(d, "executed", 0))
    total_trades = len(trades)
    total_pnl = sum(
        float(getattr(t, "pnl", "0") or "0") for t in trades
    )
    wins = sum(1 for t in trades if float(getattr(t, "pnl", "0") or "0") > 0)
    losses = sum(1 for t in trades if float(getattr(t, "pnl", "0") or "0") < 0)
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0

    # Performance trend
    if performance:
        start_val = float(performance[0].portfolio_value)
        end_val = float(performance[-1].portfolio_value)
        perf_change_pct = ((end_val - start_val) / start_val * 100) if start_val else 0.0
    else:
        perf_change_pct = 0.0

    # Strategy breakdown
    strategy_decisions = {}
    for d in decisions:
        s = getattr(d, "strategy", "unknown")
        strategy_decisions.setdefault(s, 0)
        strategy_decisions[s] += 1

    user = (
        f"## Performance Data ({len(decisions)} decisions, {total_trades} trades)\n\n"
        f"**Period Summary:**\n"
        f"- Decisions: {total_decisions} total, {executed_decisions} executed\n"
        f"- Trades: {total_trades} (W: {wins} / L: {losses})\n"
        f"- Win Rate: {win_rate:.1f}%\n"
        f"- Net PnL: ${total_pnl:.2f}\n"
        f"- Portfolio Change: {perf_change_pct:+.2f}%\n\n"
        f"**Strategy Breakdown:**\n"
    )
    for s, count in sorted(strategy_decisions.items()):
        user += f"- {s}: {count} decisions\n"

    user += (
        f"\n**Current Configuration:**\n"
        f"- Max positions: {current_config.risk.max_positions}\n"
        f"- Max daily loss: {current_config.risk.stop_loss.max_daily_loss}\n"
        f"- Max drawdown: {current_config.risk.stop_loss.max_drawdown}\n"
        f"- AI model: {current_config.ai.model}\n"
    )

    return {"system": system, "user": user}
```

### 3.6 Job Scheduling — `bot/jobs.py`

Add the optimization job callback to `QuadBotJobs` in `src/quad/bot/jobs.py`.

```python
class QuadBotJobs:
    def __init__(self, shared_state: dict[str, Any]) -> None:
        # ... existing init ...
        self._optimizer = shared_state.get("optimizer")  # NEW

    async def job_optimization_cycle(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Run the 7-day strategy self-optimization cycle.

        Scheduled via ``run_repeating`` with ``interval_days=7`` (or the
        configured ``retrain.interval_days``).
        """
        if self._optimizer is None:
            self._log.warning("job_optimization_no_optimizer")
            return

        try:
            run = await self._optimizer.run_cycle(trigger="scheduled")

            if run.status == "skipped":
                msg = (
                    "📋 *Optimization Cycle: Skipped*\n\n"
                    f"Not enough data: {run.trades_analyzed} trades "
                    f"(min: {self._optimizer._retrain_cfg.min_trades_for_analysis})"
                )
            elif run.status == "completed":
                msg = (
                    "✅ *Optimization Cycle: Complete*\n\n"
                    f"*Run ID:* `{run.id}`\n"
                    f"*Decisions analyzed:* {run.decisions_analyzed}\n"
                    f"*Trades analyzed:* {run.trades_analyzed}\n"
                    f"*Recommendations:* {run.recommendations_count}\n"
                    f"*Applied:* {run.applied_count}\n\n"
                    f"_{run.summary_json[:200]}_"
                )
            else:
                msg = (
                    "❌ *Optimization Cycle: Failed*\n\n"
                    f"`{run.error_message}`"
                )

            await self._send_if_configured(context, msg)

        except Exception as exc:
            self._log.error("job_optimization_cycle_error", error=str(exc))
            await self._send_if_configured(
                context,
                f"❌ *Optimization Cycle Error*\n\n`{exc}`",
            )
```

### 3.7 Wiring — `bot/bot.py`

Update `QuadBot.__init__` and `_setup_jobs` in `src/quad/bot/bot.py`.

```python
class QuadBot:
    def __init__(
        self,
        config: QuadConfig,
        orchestrator: Any,
        risk_manager: Any,
        execution_engine: Any,
        market_data_engine: Any,
        db_manager: Any,
        groq_client: GroqClient,
        optimizer: Any = None,                  # NEW — optional
    ) -> None:
        # ... existing code ...
        self._optimizer = optimizer

        self._shared_state: dict[str, Any] = {
            # ... existing entries ...
            "optimizer": optimizer,              # NEW
        }
```

In `_setup_jobs()`:

```python
def _setup_jobs(self) -> None:
    jq = self._application.job_queue

    # ... existing jobs ...

    # 7-day optimisation cycle
    if self._optimizer is not None:
        interval_s = self._config.retrain.interval_days * 86400
        jq.run_repeating(
            self._job_handlers.job_optimization_cycle,
            interval=interval_s,
            first=interval_s,   # Wait one full interval before first run
            name="optimization_cycle",
        )
        self._log.info(
            "optimization_job_registered",
            interval_days=self._config.retrain.interval_days,
        )
```

Pass `optimizer` in the construction call-site (wherever `QuadBot(...)` is instantiated):

```python
bot = QuadBot(
    config=config,
    orchestrator=orchestrator,
    risk_manager=risk_manager,
    execution_engine=execution_engine,
    market_data_engine=market_data_engine,
    db_manager=db_manager,
    groq_client=groq_client,
    optimizer=optimizer,  # NEW — can be None if retrain.enabled is False
)
```

### 3.8 Error Handling / Circuit Breaker

The optimizer tracks consecutive failures internally. When `_consecutive_failures >= 3`:

1. The `is_paused` property returns `True`.
2. The scheduled job callback checks `self._optimizer.is_paused` before running.
3. If paused, the job sends an alert to the notification chat requesting manual reset.
4. Admin can call `optimizer.reset_failure_count()` via a new admin command
   (future scope — for now, restart the bot to reset).

---

## 4. Implementation Order

| Phase | Files | What to do |
|---|---|---|
| **Phase 1: Config** | `config/schema.py` | Add `RetrainConfig` sub-model with all fields + `Field()` defaults. Add `retrain: RetrainConfig` field to `QuadConfig`. |
| **Phase 2: Models** | `persistence/models.py` | Add `OptimizationRunModel` and `OptimizationRecommendationModel` dataclasses. Add index definitions. Register both in `ALL_MODELS`. Bump `SCHEMA_VERSION` to 2. |
| **Phase 3: Repos** | `persistence/repositories.py` | Add `OptimizationRunRepository` and `OptimizationRecommendationRepository` inheriting `BaseRepository[T]`. Implement domain-specific query methods. |
| **Phase 4: Prompt** | `ai/prompt.py` | Add `OPTIMIZATION_SYSTEM_PROMPT` constant and `build_optimization_prompt()` function. |
| **Phase 5: Optimizer** | `ai/optimizer.py` (NEW) | Create full `Optimizer` class with `run_cycle()` orchestrating all 4 phases. |
| **Phase 6: Job** | `bot/jobs.py` | Add `job_optimization_cycle` callback to `QuadBotJobs`. |
| **Phase 7: Wire** | `bot/bot.py` | Add `optimizer` parameter to `__init__` and `shared_state`. Register job in `_setup_jobs()`. |

---

## 5. Files to Create / Modify

| File | Action |
|---|---|
| `src/quad/config/schema.py` | MODIFY — add `RetrainConfig` + field on `QuadConfig` |
| `src/quad/persistence/models.py` | MODIFY — add 2 new dataclass models, indexes, bump schema version |
| `src/quad/persistence/repositories.py` | MODIFY — add 2 new repository classes |
| `src/quad/ai/optimizer.py` | **CREATE** — `Optimizer` class with 4-phase cycle |
| `src/quad/ai/prompt.py` | MODIFY — add optimization prompt + builder function |
| `src/quad/bot/jobs.py` | MODIFY — add `job_optimization_cycle` callback |
| `src/quad/bot/bot.py` | MODIFY — wire optimizer into `__init__`, `shared_state`, `_setup_jobs()` |

---

## 6. Dependencies

- **Internal:** `quad.config.schema`, `quad.persistence.models`, `quad.persistence.repositories`,
  `quad.ai.groq`, `quad.ai.prompt`
- **External (none new):** `structlog`, `python-telegram-bot` (already present), `groq` (already present)

The optimizer uses the same GroqClient as the trading loop — no additional API keys or services needed.

---

## 7. Open Questions

1. **Backtesting integration** — Should the optimizer also run backtests of the recommended
   changes before applying them? This would require a backtesting engine, which is out of
   scope for the initial implementation.

2. **Rollback capability** — If an auto-applied recommendation causes losses, is there a
   rollback mechanism? The initial design logs changes via `ConfigChangeModel` but does not
   auto-rollback. A future enhancement could compare pre/post performance and revert if
   performance degrades.

3. **Analysis fallback on API failure** — If Groq is rate-limited or unavailable, should the
   optimizer fall back to a rule-based heuristic (e.g., "if win rate < 40%, tighten risk
   thresholds by 5%")? The initial implementation skips the cycle on failure.

4. **Admin confirmation** — Should recommendations require admin confirmation via Telegram
   before being applied (even with `auto_apply=True`)? The current design has a hard toggle:
   either fully auto or no application. A "confirm" mode could ask an admin in chat.

5. **Recommendation deduplication** — If the same recommendation appears across multiple
   runs, should it be suppressed or flagged as persistent? Currently each run produces
   independent recommendations — no cross-run dedup.

6. **Performance impact** — The `decisions_analyzed` and `trades_analyzed` counts could be
   large if `max_history_days` is high. Consider pagination or pre-aggregation if the query
   becomes a bottleneck.

7. **Prompt override persistence** — If the optimizer updates `system_prompt_override`,
   should this be persisted across bot restarts? The config file itself would need to be
   re-written, or the override stored in the `strategy_state` / `config_changes` table.
