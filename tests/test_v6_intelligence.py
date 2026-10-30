"""Integration tests for the intelligent planning layers.

Covers the scenario-suite additions that cross module boundaries:

- the gated LLM refinement of :class:`AnalyticsPlan` (invalid JSON, timeouts
  and invented dimensions fall back to the deterministic plan);
- the optional LLM visualization-recommender stage whose proposals must
  survive deterministic ``plan_chart`` validation;
- governed derived metrics that only exist through approved catalog
  definitions (no invented formulas, no averages of averages);
- fail-closed authorization of the filter-values endpoint against verified
  column profiles (sensitive fields never leak their distinct values).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient

from semantic_text2sql.agent import TextToSQLAgent
from semantic_text2sql.analytics_planner import (
    AnalyticsPlan,
    RequestedAnalysis,
    plan_analytics,
    plan_fields_known,
    plan_needs_refinement,
    planner_operational,
    refine_plan_with_model,
)
from semantic_text2sql.api import create_app
from semantic_text2sql.data_requirement_validator import validate_reusability
from semantic_text2sql.models import (
    ColumnProfile,
    DatabaseProfile,
    TokenUsage,
)
from semantic_text2sql.semantic_catalog import (
    ApprovalRecord,
    MetricCatalogEntry,
)
from semantic_text2sql.visualization import VisualizationIntent
from semantic_text2sql.visualization_recommender import (
    merge_recommendations,
    recommend_charts,
    refine_charts_with_model,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _FixedResponse:
    """Completer stub that answers every prompt with one fixed payload."""

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.model: str | None = None

    async def complete(self, model: str, prompt: str) -> str:
        self.model = model
        return self.payload

    async def complete_detailed(self, model: str, prompt: str) -> tuple[str, TokenUsage]:
        await self.complete(model=model, prompt=prompt)
        return self.payload, TokenUsage()


class _HangingCompleter:
    """Completer stub that never answers within any realistic budget."""

    async def complete(self, model: str, prompt: str) -> str:
        del model, prompt
        await asyncio.sleep(5)
        return "{}"


def _previous(
    sql: str = "SELECT month, revenue FROM revenue_view",
    columns: str = "month, revenue",
) -> Any:
    return type(
        "PreviousState",
        (),
        {
            "root_question": "Monthly revenue in 2025",
            "resolved_question": "Show monthly revenue in 2025",
            "last_sql": sql,
            "last_columns": columns.split(", "),
            "db_id": "shop",
        },
    )()


# --- planner refinement gate --------------------------------------------------


class TestAnalyticsPlannerRefinement:
    async def _accepted(self) -> None:
        payload = json.dumps(
            {
                "operation": "CHANGE_GRAIN_AND_VISUALIZE",
                "depends_on_previous": True,
                "requested_analysis": {"time_grain": "monthly", "metrics": ["revenue"]},
                "semantic_changes": [],
                "visualization_intent": {"requested": True},
            }
        )
        completer = _FixedResponse(payload=payload)
        original = plan_analytics(
            "plot it per month",
            viz_intent=VisualizationIntent(requested=True),
            previous=_previous(),
            db_id="shop",
        )
        refined, usage, _ = await refine_plan_with_model(
            completer,
            provider="agentrouter",
            model="test-model",
            message="Plot it per month.",
            previous=_previous(),
            plan=original,
        )
        assert refined.source == "model"
        assert refined.operation == "CHANGE_GRAIN_AND_VISUALIZE"
        assert refined.depends_on_previous is True
        assert refined.requested_analysis.time_grain == "monthly"
        assert usage.total_tokens is None or usage.total_tokens >= 0

    def test_valid_model_output_is_accepted_as_typed_plan(self) -> None:
        _run(self._accepted())

    async def _fallback(self) -> None:
        original = plan_analytics(
            "plot it per month",
            viz_intent=VisualizationIntent(requested=True),
            previous=_previous(),
            db_id="shop",
        )
        completer = _FixedResponse(payload="not json at all")
        refined, _, _ = await refine_plan_with_model(
            completer, provider="agentrouter", model="m",
            message="Plot it per month.", previous=_previous(), plan=original,
        )
        assert refined.source == "fallback"
        assert refined.model_copy(update={"source": "deterministic"}) == original

    def test_invalid_json_falls_back_to_deterministic_plan(self) -> None:
        _run(self._fallback())

    async def _timeout(self) -> None:
        original = plan_analytics(
            "revenue per region this side",
            viz_intent=VisualizationIntent(requested=True),
            previous=_previous(),
            db_id="shop",
        )
        refined, _, _ = await refine_plan_with_model(
            _HangingCompleter(), provider="agentrouter", model="m",
            message="whatever", previous=_previous(), plan=original,
            timeout_seconds=0.05,
        )
        assert refined.source == "fallback"
        assert refined.model_copy(update={"source": "deterministic"}) == original

    def test_timeout_falls_back_to_deterministic_plan(self) -> None:
        _run(self._timeout())

    def test_unknown_dimensions_fail_the_schema_gate(self) -> None:
        invented = plan_analytics(
            "revenue per region this side",
            viz_intent=VisualizationIntent(requested=True),
            previous=_previous(),
            db_id="shop",
        )
        invented = invented.model_copy(
            update={
                "semantic_changes": [],
                "requested_analysis": invented.requested_analysis.model_copy(
                    update={"dimensions": ["region"], "metrics": ["revenue"]}
                ),
            }
        )
        known = plan_fields_known(
            invented, {"month", "revenue", "cost", "profit", "country"}
        )
        assert known is False

    def test_known_dimensions_pass_the_schema_gate(self) -> None:
        plan = plan_analytics(
            "revenue by country",
            viz_intent=VisualizationIntent(requested=True),
            previous=_previous(),
            db_id="shop",
        )
        assert plan.requested_analysis.dimensions == ["country"]
        assert plan_fields_known(
            plan, {"month", "revenue", "cost", "profit", "country"}
        )

    def test_obvious_chart_mechanics_never_need_the_model(self) -> None:
        mechanical = AnalyticsPlan(operation="VISUALIZE_EXISTING", clarify=False)
        assert plan_needs_refinement(mechanical) is False
        structured = AnalyticsPlan(
            operation=cast(Any, "NEW_ANALYTICAL_QUERY"),
            requested_analysis=RequestedAnalysis(metrics=["revenue"]),
        )
        assert plan_needs_refinement(structured) is False
        ambiguous = AnalyticsPlan(operation=cast(Any, "NEW_ANALYTICAL_QUERY"))
        assert plan_needs_refinement(ambiguous) is True
        flagged = AnalyticsPlan(operation=cast(Any, "CLARIFY"), clarify=True)
        assert plan_needs_refinement(flagged) is True

    def test_planner_operations_partitioned(self) -> None:
        assert planner_operational("CHANGE_GRAIN_AND_VISUALIZE")
        assert planner_operational("COMPARE")
        assert not planner_operational("VISUALIZE_EXISTING")
        assert not planner_operational("EXPLAIN_VISUALIZATION")


# --- visualization recommender LLM stage --------------------------------------


class TestVisualizationRecommenderLLM:
    COLUMNS = ["month", "country", "revenue", "cost", "profit", "orders", "customers",
               "avg_order_value"]
    ROWS = [[m, c, 100.0, 60.0, 40.0, 12, 8, 8.3]
            for m in ("202501", "202502", "202503") for c in ("Germany", "France")]

    @staticmethod
    def _proposal(
        chart_type: str, x: str, measures: list[str], series: str | None = None
    ) -> dict[str, Any]:
        return {
            "chart_type": chart_type,
            "x": x,
            "y": measures[0],
            "y_measures": list(measures),
            "series": series,
            "reason": "measures share currency units",
        }

    async def _valid(self) -> None:
        # This fixture has two rows per month: country must be explicit, not
        # silently inferred as an extra series for an ostensibly monthly chart.
        payload = json.dumps(
            {
                "primary_chart": self._proposal("multi_line", "month",
                                                ["revenue"], series="country"),
                "alternative_charts": [self._proposal(
                    "grouped_bar", "month", ["revenue"], series="country"
                )],
            }
        )
        completer = _FixedResponse(payload=payload)
        recommendations, _, latency = await refine_charts_with_model(
            completer, provider="agentrouter", model="m",
            question="Show useful charts", columns=self.COLUMNS, rows=self.ROWS,
        )
        assert recommendations is not None
        assert latency >= 0
        assert recommendations.primary_chart.chart_type == "multi_line"
        assert set(recommendations.primary_chart.y_measures) <= {"revenue", "cost", "profit"}
        spec_types = [item.chart_type for item in recommendations.alternative_charts]
        assert "grouped_bar" in spec_types

    def test_valid_proposals_survive_deterministic_validation(self) -> None:
        _run(self._valid())

    async def _fabricated(self) -> None:
        bad = self._proposal("multi_line", "month", ["revenue", "made_up_margin"])
        payload = json.dumps({"primary_chart": bad, "alternative_charts": []})
        completer = _FixedResponse(payload=payload)
        recommendations, _, _ = await refine_charts_with_model(
            completer, provider="agentrouter", model="m",
            question="Show useful charts", columns=self.COLUMNS, rows=self.ROWS,
        )
        assert recommendations is None

    def test_fabricated_columns_never_reach_the_ranking(self) -> None:
        _run(self._fabricated())

    async def _degraded(self) -> None:
        completer = _FixedResponse(payload="{{not a proposal")
        baseline = recommend_charts("Financial overview", self.COLUMNS, self.ROWS)
        from_model, usage, _ = await refine_charts_with_model(
            completer, provider="agentrouter", model="m",
            question="Financial overview", columns=self.COLUMNS, rows=self.ROWS,
        )
        assert from_model is None
        assert usage.total_tokens in (None, 0)
        assert baseline is not None  # deterministic ranking stays authoritative

        recommendations, _, _ = await refine_charts_with_model(
            _HangingCompleter(), provider="agentrouter", model="m",
            question="Financial overview", columns=self.COLUMNS, rows=self.ROWS,
            timeout_seconds=0.05,
        )
        assert recommendations is None

    def test_invalid_json_and_timeout_stay_deterministic(self) -> None:
        _run(self._degraded())

    def test_merge_prefers_model_and_backfills_from_deterministic(self) -> None:
        baseline = recommend_charts("Financial overview", self.COLUMNS, self.ROWS)
        assert baseline is not None
        assert merge_recommendations(None, baseline) is baseline
        primary = baseline.primary_chart.model_copy(update={"score": 1.0})
        from_model = type(baseline)(primary_chart=primary, alternative_charts=[])
        merged = merge_recommendations(from_model, baseline)
        assert merged is not None
        assert merged.primary_chart == primary
        assert len(merged.alternative_charts) <= 3
        keys = [
            (item.chart_type, item.x, item.y, item.series, tuple(item.y_measures))
            for item in merged.alternative_charts
        ]
        assert len(keys) == len(set(keys))  # deterministic backfill never duplicates


# --- governed derived metrics --------------------------------------------------


def _approved_profit_margin() -> dict[str, MetricCatalogEntry]:
    approval = ApprovalRecord(
        approved_by="analyst",
        approved_at=datetime.now(UTC),
        approval_source="human_review",
    )
    entry = MetricCatalogEntry(
        id="profit_margin",
        label="Profit Margin",
        description="Profit divided by revenue.",
        source_relation="orders",
        formula="profit / revenue",
        required_fields=["profit", "revenue"],
        dimensions=["country"],
        valid_time_grains=["monthly"],
        unit="ratio",
        format="percent",
        status="approved",
        approval=approval,
    )
    return {"profit_margin": entry}


class TestGovernedDerivedMetrics:
    def test_approved_metric_recognized(self) -> None:
        plan = plan_analytics(
            "Show Profit Margin by country",
            viz_intent=VisualizationIntent(requested=True), previous=None,
            db_id="shop", approved_metrics=_approved_profit_margin(),
        )
        assert plan.requested_analysis.derived_measures == ["profit_margin"]
        targets = {change.target for change in plan.semantic_changes
                   if change.operation == "ADD_MEASURE"}
        assert "profit_margin" in targets

    def test_unapproved_formulas_are_never_invented(self) -> None:
        plan = plan_analytics(
            "Show Profit Margin by country",
            viz_intent=VisualizationIntent(requested=True), previous=None,
            db_id="shop", approved_metrics=None,
        )
        assert plan.requested_analysis.derived_measures == []
        assert all(change.operation != "ADD_MEASURE" for change in plan.semantic_changes)

    def test_derived_metrics_always_requery_with_lineage(self) -> None:
        plan = plan_analytics(
            "Show Profit Margin by country",
            viz_intent=VisualizationIntent(requested=True), previous=None,
            db_id="shop", approved_metrics=_approved_profit_margin(),
        )
        gate = validate_reusability(
            plan, stored_columns=["month", "revenue", "profit"],
            stored_rows=[["202501", 200.0, 50.0]],
            stored_row_count=1, stored_truncated=False,
            approved_metrics=_approved_profit_margin(),
        )
        assert gate.decision == "requery_database"
        assert "profit_margin" in gate.reason
        assert gate.additivity_note and "non-additive" in gate.additivity_note.casefold()


# --- filter-value authorization ------------------------------------------------


def _sensitive_profile(tmp_path: Path) -> Path:
    profile = DatabaseProfile(
        db_id="shop",
        dialect="sqlite",
        profiled_at=datetime.now(UTC).isoformat(),
        columns=[
            ColumnProfile(
                table="customers", column="name", database_type="sqlite",
                semantic_type="email", row_count=2, null_count=0, null_ratio=0.0,
            ),
        ],
    )
    root = tmp_path / "profiles"
    root.mkdir(exist_ok=True)
    path = root / "sqlite__shop.json"
    path.write_text(profile.model_dump_json())
    return root


def _client_with_profile(registry, monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    profile_root = _sensitive_profile(tmp_path)
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    monkeypatch.setenv("TEXT2SQL_PROFILE_ROOT", str(profile_root))
    monkeypatch.setenv("TEXT2SQL_GLOSSARY_ROOT", str(tmp_path / "glossaries"))
    return TestClient(create_app(TextToSQLAgent(registry, type(
        "FixedSQL", (), {"sql": "", "generate": None},
    )())))


class TestFilterValueAuthorization:
    def test_sensitive_profile_column_never_leaks_values(
        self, registry, monkeypatch, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        client = _client_with_profile(registry, monkeypatch, tmp_path)
        response = client.get(
            "/api/databases/shop/filter-values",
            params={"table": "customers", "column": "name"},
        )
        assert response.status_code == 403
        assert response.json() == {"detail": "This field is not available for value discovery."}
        # The authorized field still resolves normally.
        ok = client.get(
            "/api/databases/shop/filter-values",
            params={"table": "customers", "column": "country"},
        )
        assert ok.status_code == 200
        assert ok.json()["values"] == ["Germany", "Italy"]
