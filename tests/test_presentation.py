import asyncio
import json

import pytest

from semantic_text2sql.models import ResultPresentation, TokenUsage
from semantic_text2sql.presentation import present_result, validate_chart
from semantic_text2sql.runtime import RequestBudget


class Presenter:
    def __init__(self):
        self.prompts = []

    async def complete_detailed(self, **kwargs):
        self.prompts.append(kwargs["prompt"])
        if len(self.prompts) == 1:
            return json.dumps({
                "chart_type": "bar", "x": "Currency", "y": "Count",
                "title": "Customers by currency", "x_label": "Currency",
                "y_label": "Customers", "number_format": "plain",
                "reason": "Compare currency categories.",
            }), TokenUsage(input_tokens=30, output_tokens=20)
        return json.dumps({"summary": "EUR has 2 customers; CZK has 30 customers."}), (
            TokenUsage(input_tokens=10, output_tokens=8)
        )


def test_presentation_uses_shared_budget_and_reports_usage():
    budget = RequestBudget(timeout_seconds=5)
    presenter = Presenter()
    result, usage, entries = asyncio.run(
        present_result(
            presenter,
            question="Counts by currency?",
            sql="SELECT ...",
            columns=["Currency", "Count"],
            rows=[["EUR", 2], ["CZK", 30]],
            truncated=False,
            model="test",
            provider="test",
            budget=budget,
        )
    )
    assert result.chart_type == "bar"
    assert result.chart_reason == "Compare currency categories."
    assert result.title == "Customers by currency"
    assert result.y_label == "Customers"
    assert result.number_format == "plain"
    assert result.summary == "EUR has 2 customers; CZK has 30 customers."
    assert usage.total_tokens == 68
    assert budget.model_calls == 2
    assert [entry.component for entry in entries] == ["chart_selection", "result_interpretation"]
    assert all(entry.status == "SUCCEEDED" for entry in entries)
    assert '"chart_type": "bar"' in presenter.prompts[1]
    assert "time grain" in presenter.prompts[0]
    assert "number_format" in presenter.prompts[0]
    assert "time_format" in presenter.prompts[0]


def test_presentation_failure_preserves_scalar_and_partial_warning():
    result, _, entries = asyncio.run(
        present_result(
            None,
            question="Total?",
            sql="SELECT ...",
            columns=["Total"],
            rows=[[42]],
            truncated=True,
            model="test",
            provider="test",
            budget=RequestBudget(timeout_seconds=5),
        )
    )
    assert result.summary == "Total: 42"
    assert result.chart_type == "none"
    assert "Partial" in result.note
    assert [entry.status for entry in entries] == ["FAILED", "FAILED"]


def test_rejected_chart_does_not_change_the_interpreted_rows():
    class InvalidChartPresenter(Presenter):
        async def complete_detailed(self, **kwargs):
            self.prompts.append(kwargs["prompt"])
            if len(self.prompts) == 1:
                return json.dumps({
                    "chart_type": "bar", "x": "invented", "y": "Count",
                    "title": "Wrong chart", "reason": "Invalid column.",
                }), TokenUsage(input_tokens=3, output_tokens=4)
            return '{"summary":"Two categories are shown."}', TokenUsage(
                input_tokens=5, output_tokens=6
            )

    presenter = InvalidChartPresenter()
    result, usage, entries = asyncio.run(present_result(
        presenter,
        question="Counts by currency?", sql="SELECT ...",
        columns=["Currency", "Count"], rows=[["EUR", 2], ["CZK", 30]],
        truncated=False, model="test", provider="test", budget=RequestBudget(timeout_seconds=5),
    ))
    assert result.chart_type == "none"
    assert result.summary == "Two categories are shown."
    assert [entry.status for entry in entries] == ["FAILED", "SUCCEEDED"]
    assert '"chart_type": "none"' in presenter.prompts[1]
    assert usage.total_tokens == 18


def test_large_result_sends_only_ten_rows_to_presentation_model():
    presenter = Presenter()
    rows = [[f"Category {index}", index] for index in range(25)]
    result, _, _ = asyncio.run(present_result(
        presenter,
        question="Compare categories", sql="SELECT ...",
        columns=["Currency", "Count"], rows=rows,
        truncated=False, model="test", provider="test",
        budget=RequestBudget(timeout_seconds=5),
    ))
    assert "first 10 returned rows" in result.note
    for prompt in presenter.prompts:
        assert "Category 9" in prompt
        assert "Category 10" not in prompt
    assert '"returned_row_count": 25' in presenter.prompts[0]


@pytest.mark.parametrize(
    "chart,x,y,rows",
    [
        ("bar", "missing", "Count", [["A", 2], ["B", 3]]),
        ("bar", "Currency", "Count", [["A", 2], ["A", 3]]),
        ("line", "Currency", "Count", [["A", 2], ["B", 3]]),
        ("bar", "Currency", "Count", [["A", None], ["B", None]]),
    ],
)
def test_reject_invalid_chart(chart, x, y, rows):
    with pytest.raises(ValueError):
        validate_chart(
            ResultPresentation(summary="test", chart_type=chart, x=x, y=y),
            ["Currency", "Count"],
            rows,
        )
