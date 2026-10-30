"""Provisional designs use the same chart validator as fallback LLM designs."""
from __future__ import annotations

import asyncio
import json
from statistics import median
from time import perf_counter
from typing import Any

import pytest
from pydantic import ValidationError

from semantic_text2sql.conversational_agent import (
    AGENT_DECISION_SCHEMA,
    AgentDecision,
    _consistency_issue_lines,
)
from semantic_text2sql.models import ChatResponse, GenerateResponse, StrategyHints, TokenUsage
from semantic_text2sql.plotting_service import design_chart
from semantic_text2sql.runtime import RequestBudget
from semantic_text2sql.visualization_recommender import LLMChartProposal, validate_chart_proposal


def result(**updates: Any) -> ChatResponse:
    generation = GenerateResponse(
        db_id="demo", question="Revenue by month", provider="agentrouter", model="test",
        strategy=StrategyHints(mode="exact"), sql="SELECT month, revenue FROM sales",
        accepted=True, termination_reason="accepted", columns=["month", "revenue"],
        rows=[["202601", 100.0], ["202602", 120.0]], row_count=2,
    ).model_copy(update=updates)
    return ChatResponse(session_id="chart", result_id="source", operation="NEW_QUERY",
                        resolved_question="Revenue by month", message="Revenue",
                        generation=generation)


class NoModel:
    async def complete_detailed(self, **kwargs: Any) -> Any:
        raise AssertionError("A deterministic chart path must not call the model")


@pytest.mark.parametrize("updates,expected", [
    ({}, "CHART_READY"),
    ({"accepted": False}, "REQUERY_REQUIRED"),
    ({"truncated": True}, "REQUERY_REQUIRED"),
    ({"row_count": 3}, "REQUERY_REQUIRED"),
])
def test_provisional_chart_requires_complete_accepted_data(
    updates: dict[str, Any], expected: str,
) -> None:
    budget = RequestBudget(timeout_seconds=5, max_model_calls=0)
    outcome = asyncio.run(design_chart(
        result=result(**updates), objective="Plot revenue by month", completer=NoModel(),
        provider="agentrouter", model="test", budget=budget, fresh_result=True,
        provisional_proposal=LLMChartProposal(chart_type="line", x="month", y="revenue"),
    ))
    assert outcome.status == expected
    assert budget.model_calls == 0
    if outcome.spec:
        assert [row["revenue"] for row in outcome.spec.data] == [100.0, 120.0]


@pytest.mark.parametrize("columns,rows,proposal", [
    (["month", "revenue"], [["202601", 1.0], ["202601", 2.0]],
     {"chart_type": "line", "x": "month", "y": "revenue"}),
    (["month", "revenue"], [["202601", "unknown"], ["202602", "unknown"]],
     {"chart_type": "line", "x": "month", "y": "revenue"}),
    (["month", "revenue", "profit_ratio"], [["202601", 100.0, .2], ["202602", 120.0, .3]],
     {"chart_type": "multi_line", "x": "month", "y": "revenue",
      "y_measures": ["revenue", "profit_ratio"]}),
])
def test_duplicate_grain_nonnumeric_and_incompatible_units_reject_proposals(
    columns: list[str], rows: list[list[Any]], proposal: dict[str, Any],
) -> None:
    assert validate_chart_proposal(
        LLMChartProposal.model_validate(proposal), columns=columns, rows=rows,
        question="Plot it", truncated=False, score=1,
    ) is None


def test_decision_chart_schema_reuses_existing_contract_and_rejects_code() -> None:
    properties = AGENT_DECISION_SCHEMA["properties"]
    chart_schema = properties["chart_proposal"]["anyOf"][0]  # type: ignore[index]
    assert chart_schema["additionalProperties"] is False
    assert set(chart_schema["properties"]) == set(LLMChartProposal.model_fields)
    assert set(chart_schema["required"]) == set(LLMChartProposal.model_fields)
    with pytest.raises(ValidationError):
        AgentDecision.model_validate({
            "action": "CALL_TOOL", "intent": "NEW_QUERY", "tool": "ask_data",
            "chart_proposal": {"chart_type": "line", "javascript": "alert(1)"},
        })
    invalid = AgentDecision(action="ANSWER", intent="DIRECT_ANSWER", answer="Done",
                            chart_proposal=LLMChartProposal(chart_type="bar", x="x", y="y"))
    assert any("chart_proposal" in issue for issue in _consistency_issue_lines(invalid))


def test_compatible_multiple_measures_preserve_exact_encodings() -> None:
    proposal = LLMChartProposal(chart_type="multi_line", x="month", y="revenue",
                                y_measures=["revenue", "cost", "profit"])
    accepted = validate_chart_proposal(
        proposal, columns=["month", "revenue", "cost", "profit"],
        rows=[["202601", 100.0, 60.0, 40.0], ["202602", 120.0, 80.0, 40.0]],
        question="Plot it", truncated=False, score=1,
    )
    assert accepted is not None
    assert accepted.y_measures == ["revenue", "cost", "profit"]
    # An omitted series must not be silently invented to resolve duplicate X.
    rejected = validate_chart_proposal(
        proposal, columns=["month", "revenue", "cost", "profit", "country"],
        rows=[["202601", 100.0, 60.0, 40.0, "Germany"],
              ["202601", 120.0, 80.0, 40.0, "France"]],
        question="Plot it", truncated=False, score=1,
    )
    assert rejected is None


def test_controlled_planning_latency_and_call_counts() -> None:
    """Reproducible service microbenchmark; not a production-latency assertion."""
    proposal = LLMChartProposal(chart_type="line", x="month", y="revenue")

    class DelayedModel:
        calls = 0

        async def complete_detailed(self, **kwargs: Any) -> tuple[str, TokenUsage]:
            self.calls += 1
            await asyncio.sleep(.005)
            return json.dumps({"primary_chart": proposal.model_dump(),
                               "alternative_charts": []}), TokenUsage()

    async def measure(integrated: bool) -> tuple[int, list[float]]:
        model = DelayedModel()
        latencies = []
        for _ in range(30):
            started = perf_counter()
            outcome = await design_chart(
                result=result(), objective="Plot revenue by month", completer=model,
                provider="agentrouter", model="test", fresh_result=True,
                budget=RequestBudget(timeout_seconds=5, max_model_calls=1),
                provisional_proposal=proposal if integrated else None,
            )
            latencies.append((perf_counter() - started) * 1000)
            assert outcome.status == "CHART_READY"
            assert outcome.spec and outcome.spec.y == "revenue"
        return model.calls, latencies

    for integrated in (False, True):
        calls, durations = asyncio.run(measure(integrated))
        assert calls == (0 if integrated else 30)
        print(f"integrated={integrated}: calls={calls}, "
              f"p50={median(durations):.3f}ms p95={sorted(durations)[28]:.3f}ms")
