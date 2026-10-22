import asyncio
import json

import pytest

from semantic_text2sql.models import ChartSpec, TokenUsage
from semantic_text2sql.presentation import present_result
from semantic_text2sql.runtime import RequestBudget
from semantic_text2sql.visualization import (
    VisualizationError,
    parse_intent,
    plan_chart,
)


class Presenter:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete_detailed(self, **kwargs):  # type: ignore[no-untyped-def]
        self.prompts.append(kwargs["prompt"])
        return json.dumps({"summary": "Two currencies are represented."}), TokenUsage(
            input_tokens=10, output_tokens=8
        )


def _present(question: str, *, truncated: bool = False):  # type: ignore[no-untyped-def]
    presenter = Presenter()
    budget = RequestBudget(timeout_seconds=5)
    result, usage, entries = asyncio.run(
        present_result(
            presenter,
            question=question,
            sql="SELECT Currency, Count FROM counts",
            columns=["Currency", "Count"],
            rows=[["EUR", 2], ["CZK", 30]],
            truncated=truncated,
            model="test",
            provider="test",
            budget=budget,
            visualization=parse_intent(question),
        )
    )
    return result, usage, entries, presenter, budget


def test_ordinary_question_calls_no_chart_model_and_has_no_chart() -> None:
    result, usage, entries, presenter, budget = _present("Show counts by currency.")
    assert result.chart_spec is None
    assert result.chart_type == "none"
    assert result.summary == "Two currencies are represented."
    assert usage.total_tokens == 18
    assert budget.model_calls == 1
    assert [entry.component for entry in entries] == ["result_interpretation"]
    assert "chart_type" not in presenter.prompts[0]


def test_explicit_chart_is_deterministic_and_uses_full_result() -> None:
    result, _, entries, _, budget = _present("Plot counts by currency as a bar chart.")
    assert result.chart_type == "bar"
    assert result.chart_spec is not None
    assert result.chart_spec.data == [
        {"Currency": "EUR", "Count": 2},
        {"Currency": "CZK", "Count": 30},
    ]
    assert budget.model_calls == 1
    assert [entry.component for entry in entries] == ["result_interpretation"]


def test_truncated_result_is_not_plotted() -> None:
    result, _, _, _, _ = _present("Plot counts by currency.", truncated=True)
    assert result.chart_spec is None
    assert "truncated" in result.chart_reason


@pytest.mark.parametrize(
    "word,columns,rows",
    [
        ("line chart", ["Month", "Revenue"], [["2026-01", 4], ["2026-02", 5]]),
        ("area chart", ["Month", "Revenue"], [["2026-01", 4], ["2026-02", 5]]),
        ("horizontal bar chart", ["Country", "Revenue"], [["DE", 4], ["FR", 5]]),
        (
            "grouped bar chart",
            ["Month", "Country", "Revenue"],
            [["Jan", "DE", 4], ["Jan", "FR", 5]],
        ),
        (
            "stacked bar chart",
            ["Month", "Country", "Revenue"],
            [["Jan", "DE", 4], ["Jan", "FR", 5]],
        ),
        (
            "stacked area chart",
            ["Month", "Country", "Revenue"],
            [["2026-01", "DE", 4], ["2026-01", "FR", 5]],
        ),
        ("scatter plot", ["Age", "Spend"], [[25, 40], [30, 50]]),
        ("bubble plot", ["Age", "Spend", "Count"], [[25, 40, 5], [30, 50, 6]]),
        ("histogram", ["Spend"], [[40], [50], [60]]),
        ("box plot", ["Spend"], [[1], [2], [3], [4], [5]]),
        ("heatmap", ["Country", "Month", "Revenue"], [["DE", "Jan", 4], ["FR", "Feb", 5]]),
        ("pie chart", ["Country", "Revenue"], [["DE", 4], ["FR", 5]]),
        ("donut chart", ["Country", "Revenue"], [["DE", 4], ["FR", 5]]),
        ("treemap", ["Region", "Country", "Revenue"], [["EU", "DE", 4], ["EU", "FR", 5]]),
        ("calendar heatmap", ["Date", "Revenue"], [["2026-01-01", 4], ["2026-01-02", 5]]),
    ],
)
def test_chart_families(word: str, columns: list[str], rows: list[list[object]]) -> None:
    intent = parse_intent(f"Create a {word}.")
    assert intent.chart_type is not None
    spec = plan_chart(
        intent, question=f"Create a {word}.", columns=columns, rows=rows, truncated=False
    )
    assert spec.chart_type == intent.chart_type
    assert spec.data


def test_duplicate_grain_null_and_truncation_are_rejected() -> None:
    intent = parse_intent("Create a stacked bar chart.")
    with pytest.raises(VisualizationError, match="Multiple rows"):
        plan_chart(
            intent,
            question="stacked bar chart",
            columns=["Month", "Country", "Revenue"],
            rows=[["Jan", "DE", 4], ["Jan", "DE", 5]],
            truncated=False,
        )
    with pytest.raises(VisualizationError, match="NULL"):
        plan_chart(
            parse_intent("Plot a bar chart"),
            question="bar chart",
            columns=["Country", "Revenue"],
            rows=[["DE", 4], ["FR", None]],
            truncated=False,
        )
    with pytest.raises(VisualizationError, match="truncated"):
        plan_chart(
            parse_intent("Plot a bar chart"),
            question="bar chart",
            columns=["Country", "Revenue"],
            rows=[["DE", 4]],
            truncated=True,
        )


def test_intent_distinguishes_show_from_plot_and_followup() -> None:
    assert not parse_intent("Show revenue by month in 2025.").requested
    assert parse_intent("Plot monthly revenue in 2025.").requested
    assert parse_intent("Make it a line chart instead.", has_previous=True).followup


def test_percent_stacked_chart_intent_and_complete_result() -> None:
    question = "Plot a 100% stacked bar chart by country and month."
    intent = parse_intent(question)
    assert intent.chart_type == "percent_stacked_bar"
    spec = plan_chart(
        intent,
        question=question,
        columns=["Month", "Country", "Revenue"],
        rows=[
            ["2026-01", "DE", 10], ["2026-01", "FR", 20],
            ["2026-02", "DE", 15], ["2026-02", "FR", 25],
        ],
        truncated=False,
    )
    assert spec.chart_type == "percent_stacked_bar"


def test_chart_spec_rejects_unverified_or_incompatible_multi_measures() -> None:
    from pydantic import ValidationError

    base = dict(
        chart_type="multi_line", x="Month", y="Revenue", title="Results",
        x_label="Month", y_label="Money", source_columns=["Month", "Revenue", "Profit"],
        data=[{"Month": "2026-01", "Revenue": 10, "Profit": 4}],
    )
    spec = ChartSpec(
        **base, y_measures=["Revenue", "Profit"],
        measure_units={"Revenue": "EUR", "Profit": "EUR"},
    )
    assert spec.y_measures == ["Revenue", "Profit"]
    with pytest.raises(ValidationError):
        ChartSpec(**base, y_measures=["Revenue", "Orders"])
    with pytest.raises(ValidationError):
        ChartSpec(**base, y_measures=["Revenue", "Profit"],
                  measure_units={"Revenue": "EUR", "Profit": "count"})


def test_intent_axis_extraction_from_per_by_and_multi_dimensions() -> None:
    intent = parse_intent("Plot the number of races per year as a line chart.")
    assert intent.chart_type == "line"
    assert intent.x == "year"

    grouped = parse_intent("Plot revenue by month and country.")
    assert grouped.x == "month"
    assert grouped.series == "country"

    stacked = parse_intent("Stacked bar chart of monthly revenue by country.")
    assert stacked.chart_type == "stacked_bar"
    assert stacked.series == "country"

    scatter = parse_intent("Scatter plot of spending against transaction count.")
    assert scatter.chart_type == "scatter"
    assert scatter.y == "transaction count"


def test_unmentioned_aggregate_dimension_becomes_the_remaining_numeric_axis() -> None:
    """The aggregate alias does not always echo question wording (race_count, total_orders)."""

    intent = parse_intent("Plot the number of races per year as a line chart.")
    spec = plan_chart(
        intent,
        question="Plot the number of races per year as a line chart.",
        columns=["year", "race_count"],
        rows=[[1950, 7], [1951, 8]],
        truncated=False,
    )
    assert spec.chart_type == "line"
    assert spec.x == "year"
    assert spec.y == "race_count"


def test_precomputed_box_summary_requires_ordered_five_numbers() -> None:
    columns = ["segment", "min", "q1", "median", "q3", "max"]
    intent = parse_intent("Create a box plot of spending by segment.")
    spec = plan_chart(
        intent, question="Create a box plot of spending by segment.", columns=columns,
        rows=[["SME", 1, 2, 3, 4, 5]], truncated=False,
    )
    assert spec.data[0]["median"] == 3
    assert spec.data[0]["group"] == "SME"
    with pytest.raises(VisualizationError, match="ordered"):
        plan_chart(
            intent, question="Create a box plot of spending by segment.", columns=columns,
            rows=[["SME", 1, 4, 3, 2, 5]], truncated=False,
        )
