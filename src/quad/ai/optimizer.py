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

from quad.config.schema import AiConfig, QuadConfig
from quad.ai.groq import GroqClient
from quad.ai.prompt import build_optimization_prompt
from quad.persistence.models import (
    ConfigChangeModel,
    OptimizationRunModel,
    OptimizationRecommendationModel,
)
from quad.persistence.repositories import (
    ConfigChangeRepository,
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
    config_change_repo:
        Optional repository for auditing config changes.
    config_dict:
        Optional mutable config dict to update in-place when recommendations
        are applied.  If ``None`` only the internal ``_config`` Pydantic
        model is updated.
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
        config_change_repo: ConfigChangeRepository | None = None,
        config_dict: dict[str, Any] | None = None,
    ) -> None:
        self._config = config
        self._retrain_cfg = config.retrain
        self._groq = groq_client
        self._decision_repo = decision_repo
        self._trade_repo = trade_repo
        self._perf_repo = performance_repo
        self._run_repo = run_repo
        self._rec_repo = recommendation_repo
        self._config_change_repo = config_change_repo
        self._config_dict = config_dict
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
        run_id = await self._run_repo.create(run)
        run.id = run_id
        self._log = self._log.bind(run_id=run_id)

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

        Based on ``rec.recommendation_type``:

        * ``parameter_adjustment`` -- update the internal Pydantic model and
          optionally the mutable ``config_dict`` passed at construction.
        * ``prompt_update`` -- update ``AiConfig.system_prompt_override``.
        * ``risk_threshold`` -- update risk config values.
        * ``strategy_toggle`` -- toggle a strategy's enabled flag.

        Every change is logged to the ``config_changes`` audit table if a
        ``ConfigChangeRepository`` was provided.
        """
        rec_type = rec.recommendation_type
        target = rec.target_area
        new_value_str = rec.recommended_value
        now_ms = int(time.time() * 1000)

        self._log.info(
            "optimization_applying",
            rec_id=rec.id,
            rec_type=rec_type,
            target=target,
            to=new_value_str,
        )

        # --- Parse the recommended value from its JSON wrapper ---
        try:
            recommended = json.loads(new_value_str)
        except (json.JSONDecodeError, TypeError):
            recommended = new_value_str

        # --- Build an audit trail entry (will be persisted below) ---
        now = int(time.time())

        if rec_type == "parameter_adjustment":
            # Update strategy parameters nested under strategy.<target>
            if self._config_dict is not None:
                strategy_section = self._config_dict.setdefault("strategy", {})
                strategy_section[target] = recommended

            # Update the Pydantic config model
            params = self._config.strategy_params or {}
            params[target] = recommended
            self._config = self._config.model_copy(
                update={"strategy_params": params}
            )

            await self._log_config_change(
                key=f"strategy.{target}",
                old_value=json.dumps(params.get(target, "")),
                new_value=json.dumps(recommended),
                source=f"optimizer:rec:{rec.id}",
            )

        elif rec_type == "prompt_update":
            # Update the system prompt override
            if self._config_dict is not None:
                ai_section = self._config_dict.setdefault("ai", {})
                if target == "system_prompt":
                    ai_section["system_prompt_override"] = str(recommended)

            # Update the Pydantic AiConfig via the parent QuadConfig
            ai_cfg = self._config.ai
            if target == "system_prompt":
                ai_cfg = ai_cfg.model_copy(
                    update={"system_prompt_override": str(recommended)}
                )
            elif target == "temperature":
                try:
                    ai_cfg = ai_cfg.model_copy(
                        update={"temperature": float(recommended)}
                    )
                except (ValueError, TypeError):
                    pass
            self._config = self._config.model_copy(update={"ai": ai_cfg})

            await self._log_config_change(
                key=f"ai.{target}",
                old_value="",
                new_value=str(recommended),
                source=f"optimizer:rec:{rec.id}",
            )

        elif rec_type == "risk_threshold":
            # Update risk configuration values
            if self._config_dict is not None:
                risk_section = self._config_dict.setdefault("risk", {})
                risk_section[target] = recommended

            await self._log_config_change(
                key=f"risk.{target}",
                old_value="",
                new_value=json.dumps(recommended),
                source=f"optimizer:rec:{rec.id}",
            )

        elif rec_type == "strategy_toggle":
            # Enable / disable a strategy
            if self._config_dict is not None:
                strategy_section = self._config_dict.setdefault("strategy", {})
                strategy_section[target] = recommended

            await self._log_config_change(
                key=f"strategy.{target}.enabled",
                old_value="",
                new_value=json.dumps(recommended),
                source=f"optimizer:rec:{rec.id}",
            )

        else:
            self._log.warning(
                "optimization_unknown_rec_type",
                rec_id=rec.id,
                rec_type=rec_type,
            )

        self._log.info(
            "optimization_applied",
            rec_id=rec.id,
            target=target,
            rec_type=rec_type,
        )

    async def _log_config_change(
        self,
        key: str,
        old_value: str,
        new_value: str,
        source: str,
    ) -> None:
        """Write a config change audit entry if a repo is available."""
        if self._config_change_repo is None:
            return
        try:
            await self._config_change_repo.create(
                ConfigChangeModel(
                    id=0,
                    timestamp=int(time.time()),
                    key=key,
                    old_value=old_value,
                    new_value=new_value,
                    source=source,
                )
            )
        except Exception as exc:
            self._log.warning(
                "config_change_log_failed",
                key=key,
                error=str(exc),
            )

    @property
    def is_paused(self) -> bool:
        """Check if the optimizer has exceeded the max consecutive failure threshold."""
        return self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES

    def reset_failure_count(self) -> None:
        """Manually reset the consecutive failure counter."""
        self._consecutive_failures = 0
