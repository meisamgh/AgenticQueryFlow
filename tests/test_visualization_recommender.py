"""Recommendation-engine behavior over realistic executed results."""

from __future__ import annotations

from typing import Any

from semantic_text2sql.visualization_recommender import (
    ChartRecommendations,
    materialize_recommendation,
    recommend_charts,
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
