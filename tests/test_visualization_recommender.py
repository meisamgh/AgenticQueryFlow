"""Recommendation-engine behavior over realistic executed results."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from semantic_text2sql.models import (
    ChatResponse,
    GenerateResponse,
    RequestedChange,
    ResultPresentation,
    StrategyHints,
    TokenUsage,
)
from semantic_text2sql.plotting_service import _type_only_chart_change, design_chart
from semantic_text2sql.runtime import RequestBudget
from semantic_text2sql.visualization import VisualizationError, VisualizationIntent, plan_chart
from semantic_text2sql.visualization_recommender import (
    ChartRecommendations,
    materialize_recommendation,
    recommend_charts,
    refine_charts_with_model,
)

ROW_MONTH_COUNTRY = [
    [m, c, r, co, p, o, cu, av]
    for m in ("202501", "202502", "202503")
    for c in ("Germany", "France")
    for r, co, p, o, cu, av in ((100.0, 60.0, 40.0, 12, 8, 8.33), (120.0, 70.0, 50.0, 14, 9, 8.57))
]

SCALAR_TOTAL: list[list[Any]] = [[125_000]]
MONTHLY_REVENUE = [(m, float(100 + 20 * i)) for i, m in enumerate(
    ("202501", "202502", "202503", "202504")
)]
REVENUE_BY_COUNTRY = [
    ("Germany", 400.0), ("France", 320.0), ("Spain", 280.0), ("Italy", 240.0),
]
PRICE_CONSUMPTION = [(50.0, 100.0), (60.0, 130.0), (70.0, 170.0), (80.0, 200.0), (90.0, 240.0)]
MEASURES = [(1.0, 0.6, 0.4), (2.0, 1.2, 0.8), (3.0, 1.8, 1.2), (4.0, 2.4, 1.6)]
MONTH_COUNTRY_REVENUE = [
    ("202501", "Germany", 100.0), ("202501", "France", 80.0),
    ("202502", "Germany", 110.0), ("202502", "France", 90.0),
]


def _columns(rows: list[list[Any]], names: list[str]) -> tuple[list[str], list[list[Any]]]:
    return names, rows


def test_scenario_f_compatible_measures_chart_together() -> None:
    columns, rows = _columns(
        [("M1", 100.0, 60.0, 40.0), ("M2", 120.0, 70.0, 50.0),
         ("M3", 140.0, 80.0, 60.0), ("M4", 160.0, 90.0, 70.0)],
        ["month", "revenue", "cost", "profit"],
    )
    recommendations = recommend_charts(
        "Plot revenue, cost and profit together by month", columns, rows
    )
    assert recommendations is not None
    primary = recommendations.primary_chart
    spec = materialize_recommendation(
        primary, question="Plot revenue, cost and profit together",
        columns=columns, rows=rows, truncated=False,
    )
    # Money-family measures only, never a count or a ratio on the shared axis.
    assert set(spec.y_measures) <= {"revenue", "cost", "profit"}
    assert spec.y not in ({"orders", "customers"},)
    assert spec.chart_type in {"multi_line", "grouped_bar"}


def test_scenario_d_eight_column_result_recommends_compatible_charts() -> None:
    columns = ["month", "country", "revenue", "cost", "profit", "orders", "customers",
               "avg_order_value"]
    rows: list[list[Any]] = [[m, c, 100.0, 60.0, 40.0, 12, 8, 8.3]
                             for m in ("202501", "202502", "202503")
                             for c in ("Germany", "France")]
    recommendations = recommend_charts("Recommend useful charts", columns, rows)
    assert recommendations is not None
    all_recommendations = [recommendations.primary_chart, *recommendations.alternative_charts]
    # The year-like month values are an axis, never a measure.
    for chart in all_recommendations:
        assert chart.y in {"revenue", "cost", "profit", "orders", "customers",
                           "avg_order_value"}
        assert chart.y != "month" and chart.x != "revenue"
    assert len(recommendations.alternative_charts) >= 2
    # The ratio measure never shares an axis with currency measures.
    multi = [c for c in all_recommendations if len(c.y_measures) > 1]
    for chart in multi:
        assert "avg_order_value" not in chart.y_measures


def test_scenario_h_category_comparison_and_alternatives() -> None:
    columns, rows = _columns(REVENUE_BY_COUNTRY, ["country", "revenue"])
    recommendations = recommend_charts("Revenue by country", columns, rows)
    assert recommendations is not None
    assert recommendations.primary_chart.chart_type in {"bar", "horizontal_bar"}
    assert recommendations.primary_chart.x == "country"
    types = {c.chart_type for c in recommendations.alternative_charts}
    assert {"donut", "treemap"} & types


def test_scenario_h_time_series_variants_rank_primary_line() -> None:
    columns, rows = _columns(MONTHLY_REVENUE, ["month", "revenue"])
    recommendations = recommend_charts("Show monthly revenue over time", columns, rows)
    assert recommendations is not None
    assert recommendations.primary_chart.chart_type == "line"
    alternatives = {c.chart_type for c in recommendations.alternative_charts}
    assert {"area", "multi_line"} & alternatives


def test_scenario_h_correlation_prefers_scatter() -> None:
    columns, rows = _columns(PRICE_CONSUMPTION, ["price", "consumption"])
    recommendations = recommend_charts("Relationship between price and consumption", columns, rows)
    assert recommendations is not None
    primary = recommendations.primary_chart
    assert primary.chart_type == "scatter"
    spec = materialize_recommendation(
        primary, question="Relationship between price and consumption",
        columns=columns, rows=rows, truncated=False,
    )
    assert spec.x == "price" and spec.y == "consumption"


def test_scenario_h_distribution_recommends_histogram_when_no_categories() -> None:
    values = [(float(i), float(i * 2)) for i in range(40)]
    columns, rows = _columns(values, ["observation", "value"])
    recommendations = recommend_charts("Show the value distribution", columns, rows)
    assert recommendations is not None
    assert recommendations.primary_chart.chart_type == "histogram"


def test_scenario_h_multi_dimension_result_uses_series() -> None:
    columns, rows = _columns(MONTH_COUNTRY_REVENUE, ["month", "country", "revenue"])
    recommendations = recommend_charts("Compare revenue of countries by month", columns, rows)
    assert recommendations is not None
    primary = recommendations.primary_chart
    assert primary.series == "country"
    assert primary.chart_type in {"multi_line", "grouped_bar", "heatmap"}

    spec = materialize_recommendation(
        primary, question="Compare revenue of countries by month",
        columns=columns, rows=rows, truncated=False,
    )
    assert spec.series == "country"


def test_incompatible_measures_not_mixed_into_one_axis() -> None:
    columns, rows = _columns(
        [(1.0, 10.0, 3)], ["month", "revenue", "customer_count"]
    )
    recommendations = recommend_charts("Plot revenue and customer count", columns, rows)
    if recommendations is not None:
        measures = recommendations.primary_chart.y_measures
        if len(measures) > 1:
            assert set(measures) <= {"revenue"} | {"customer_count"} and len(set(measures)) == 1
        # Whatever remains is validated against the real rows.
        materialize_recommendation(
            recommendations.primary_chart, question="Plot revenue and customer count",
            columns=columns, rows=rows, truncated=False,
        )


def test_scalar_result_yields_no_recommendation() -> None:
    assert recommend_charts("Total", ["total_consumption"], SCALAR_TOTAL) is None


def test_year_and_month_can_form_a_verified_chart_axis_without_aggregation() -> None:
    columns = ["Year", "Month", "Revenue"]
    rows = [[2025, 12, 10.0], [2026, 1, 11.0], [2025, 11, 9.0]]
    recommendation = recommend_charts("Plot Year, Month and Revenue", columns, rows)
    assert recommendation is not None
    assert recommendation.primary_chart.x_components == ["Year", "Month"]
    spec = materialize_recommendation(
        recommendation.primary_chart, question="Plot Year, Month and Revenue",
        columns=columns, rows=rows, truncated=False,
    )
    assert spec.source_columns == columns
    assert spec.x == "Year-Month"
    assert spec.x_components == ["Year", "Month"]
    assert [row[spec.x] for row in spec.data] == ["2025-11", "2025-12", "2026-01"]
    assert [row[spec.y] for row in spec.data] == [9.0, 10.0, 11.0]


def test_invalid_or_duplicate_combined_month_is_not_silently_plotted() -> None:
    columns = ["Year", "Month", "Revenue"]
    intent = VisualizationIntent(requested=True, chart_type="line", x="Year-Month",
                                 x_components=["Year", "Month"], y="Revenue")
    for rows in ([[2025, 13, 3.0]], [[2025, 1, 3.0], [2025, 1, 4.0]]):
        try:
            plan_chart(intent, question="", columns=columns, rows=rows, truncated=False)
        except VisualizationError:
            pass
        else:
            raise AssertionError("Invalid or duplicate Year-Month chart position was accepted")


def test_explicit_plot_columns_exclude_unrequested_series() -> None:
    columns = ["Month", "Revenue", "Country"]
    rows = [["2025-01", 10.0, "DE"], ["2025-02", 12.0, "FR"]]
    recommendation = recommend_charts("Plot Month and Revenue", columns, rows)
    assert recommendation is not None
    assert recommendation.primary_chart.x == "Month"
    assert recommendation.primary_chart.y == "Revenue"
    assert recommendation.primary_chart.series is None


def test_model_can_select_combined_time_axis_but_not_unrequested_columns() -> None:
    class ChartModel:
        async def complete_detailed(self, *, model: str, prompt: str) -> tuple[str, TokenUsage]:
            assert "Year and Month" in prompt
            return json.dumps({"primary_chart": {
                "chart_type": "line", "x": "Year-Month",
                "x_components": ["Year", "Month"], "y": "Revenue",
                "y_measures": [], "series": None, "reason": "Monthly trend",
            }, "alternative_charts": [{
                "chart_type": "line", "x": "Year-Month",
                "x_components": ["Year", "Month"], "y": "Revenue",
                "y_measures": [], "series": "Country", "reason": "Unrequested series",
            }]}), TokenUsage()

    recommended, _, _ = asyncio.run(refine_charts_with_model(
        ChartModel(), provider="agentrouter", model="test",
        question="Plot Year, Month and Revenue as a line chart",
        columns=["Year", "Month", "Revenue", "Country"],
        rows=[[2025, 12, 10.0, "DE"], [2026, 1, 11.0, "FR"]],
        required_chart_type="line",
    ))
    assert recommended is not None
    assert recommended.primary_chart.x_components == ["Year", "Month"]
    assert recommended.alternative_charts == []


def test_model_chart_prompt_receives_only_a_bounded_row_sample() -> None:
    class CapturingModel:
        async def complete_detailed(self, *, model: str, prompt: str) -> tuple[str, TokenUsage]:
            assert "Row count: 20" in prompt
            assert "row_4" in prompt
            assert "row_5" not in prompt
            assert "row_19" not in prompt
            return "{}", TokenUsage()

    recommendation, _, _ = asyncio.run(refine_charts_with_model(
        CapturingModel(), provider="agentrouter", model="test",
        question="Plot the values", columns=["category", "value"],
        rows=[[f"row_{index}", float(index)] for index in range(20)],
    ))
    assert recommendation is None


def test_charting_llm_receives_previous_chart_and_typed_change() -> None:
    columns = ["country", "revenue", "cost"]
    rows = [["Germany", 100.0, 60.0], ["France", 80.0, 40.0]]
    prior = plan_chart(
        VisualizationIntent(requested=True, chart_type="bar", x="country", y="revenue"),
        question="", columns=columns, rows=rows, truncated=False,
    )
    changes = [RequestedChange(
        operation="CHANGE_VISUALIZATION", target="chart_type",
        new_value="horizontal_bar",
    )]

    class ChartModel:
        def __init__(self, measure: str) -> None:
            self.measure = measure
            self.prompts: list[str] = []

        async def complete_detailed(self, *, model: str, prompt: str) -> tuple[str, TokenUsage]:
            self.prompts.append(prompt)
            return json.dumps({
                "primary_chart": {
                    "chart_type": "horizontal_bar", "x": "country",
                    "x_components": [], "y": self.measure,
                    "y_measures": [self.measure], "series": None,
                    "reason": "Display the same country totals horizontally.",
                },
                "alternative_charts": [],
            }), TokenUsage()

    wrong = ChartModel("cost")
    rejected, _, _ = asyncio.run(refine_charts_with_model(
        wrong, provider="agentrouter", model="test",
        question="Change to a horizontal bar chart",
        columns=columns, rows=rows, required_chart_type="horizontal_bar",
        previous_chart=prior, chart_changes=changes,
        preserve_previous_encodings=True,
    ))
    assert rejected is None  # Same rows, but model silently changed the measure.
    assert '"chart_type":"bar"' in wrong.prompts[0]
    assert '"operation":"CHANGE_VISUALIZATION"' in wrong.prompts[0]
    assert "preserve ALL prior chart encodings" in wrong.prompts[0]

    correct = ChartModel("revenue")
    accepted, _, _ = asyncio.run(refine_charts_with_model(
        correct, provider="agentrouter", model="test",
        question="Change to a horizontal bar chart",
        columns=columns, rows=rows, required_chart_type="horizontal_bar",
        previous_chart=prior, chart_changes=changes,
        preserve_previous_encodings=True,
    ))
    assert accepted is not None
    assert accepted.primary_chart.chart_type == "horizontal_bar"
    assert accepted.primary_chart.x == prior.x
    assert accepted.primary_chart.y == prior.y


def test_only_unambiguous_chart_type_changes_preserve_existing_encodings() -> None:
    columns = ["country", "revenue", "cost"]
    rows = [["Germany", 100.0, 60.0], ["France", 80.0, 40.0]]
    prior = plan_chart(
        VisualizationIntent(requested=True, chart_type="bar", x="country", y="revenue"),
        question="", columns=columns, rows=rows, truncated=False,
    )
    assert _type_only_chart_change(
        "Change the chart to a horizontal bar of revenue by country",
        columns, prior, None,
    )
    assert not _type_only_chart_change(
        "Put cost on the Y-axis", columns, prior, None,
    )
    assert not _type_only_chart_change(
        "Only show Germany", columns, prior, None,
    )
    assert not _type_only_chart_change(
        "Change to a line chart", columns, prior,
        [RequestedChange(operation="CHANGE_PERIOD", target="year", new_value="2026")],
    )


def test_plotting_fallback_preserves_previous_metric_and_requeries_changes() -> None:
    columns = ["country", "revenue", "cost"]
    rows = [["Germany", 100.0, 60.0], ["France", 80.0, 40.0]]
    prior = plan_chart(
        VisualizationIntent(requested=True, chart_type="bar", x="country", y="revenue"),
        question="", columns=columns, rows=rows, truncated=False,
    )
    result = ChatResponse(
        session_id="same", operation="NEW_QUERY",
        resolved_question="Revenue by country", message="Here is revenue by country.",
        generation=GenerateResponse(
            db_id="shop", question="Revenue by country", provider="agentrouter",
            model="test", strategy=StrategyHints(mode="exact"),
            sql="SELECT country, revenue, cost FROM sales", accepted=True,
            columns=columns, rows=rows, row_count=2,
        ),
        presentation=ResultPresentation(summary="Revenue by country", chart_spec=prior),
    )

    class WrongMetricModel:
        calls = 0

        async def complete_detailed(self, *, model: str, prompt: str) -> tuple[str, TokenUsage]:
            self.calls += 1
            return json.dumps({
                "primary_chart": {
                    "chart_type": "horizontal_bar", "x": "country",
                    "x_components": [], "y": "cost", "y_measures": ["cost"],
                    "series": None, "reason": "Inappropriate measure switch.",
                }, "alternative_charts": [],
            }), TokenUsage()

    chart_model = WrongMetricModel()
    outcome = asyncio.run(design_chart(
        result=result, objective="Change the chart to a horizontal bar",
        completer=chart_model, provider="agentrouter", model="test",
        budget=RequestBudget(timeout_seconds=10, max_model_calls=3),
    ))
    assert chart_model.calls == 1
    assert outcome.status == "CHART_READY"
    assert outcome.spec is not None
    assert outcome.spec.chart_type == "horizontal_bar"
    assert outcome.spec.x == "country"
    assert outcome.spec.y == "revenue"
    assert outcome.spec.data == prior.data

    blocked = asyncio.run(design_chart(
        result=result, objective="Change to a horizontal bar for 2026",
        completer=chart_model, provider="agentrouter", model="test",
        budget=RequestBudget(timeout_seconds=10, max_model_calls=3),
        changes=[RequestedChange(operation="CHANGE_PERIOD", target="year", new_value="2026")],
    ))
    assert blocked.status == "REQUERY_REQUIRED"
    assert chart_model.calls == 1


def test_truncated_result_yields_no_recommendation() -> None:
    columns, rows = _columns(MONTHLY_REVENUE, ["month", "revenue"])
    assert recommend_charts("Plot it", columns, rows, truncated=True) is None


def test_every_recommendation_survives_validation_against_actual_rows() -> None:
    columns = ["month", "country", "revenue", "cost", "profit", "orders", "customers",
               "avg_order_value"]
    rows: list[list[Any]] = [[m, c, 100.0, 60.0, 40.0, 12, 8, 8.3]
                             for m in ("202501", "202502", "202503")
                             for c in ("Germany", "France")]
    recommendations = recommend_charts("Recommend useful charts", columns, rows)
    assert isinstance(recommendations, ChartRecommendations)
    for chart in [recommendations.primary_chart, *recommendations.alternative_charts]:
        spec = materialize_recommendation(
            chart, question="Recommend useful charts", columns=columns, rows=rows,
            truncated=False,
        )
        assert spec.source_columns == columns
        assert chart.title == spec.title
        assert chart.score > 0
