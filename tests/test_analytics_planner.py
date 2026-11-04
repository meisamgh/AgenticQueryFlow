"""Scenario tests for the deterministic analytics planner."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from semantic_text2sql.analytics_planner import (
    AnalyticsPlan,
    plan_analytics,
    planner_enabled,
    refine_plan_with_model,
)
from semantic_text2sql.conversation import ConversationState
from semantic_text2sql.models import TokenUsage
from semantic_text2sql.runtime import RequestBudget
from semantic_text2sql.visualization import parse_intent


def _previous(columns: list[str] | None = None) -> ConversationState:
    return ConversationState(
        session_id="session-1",
        db_id="books",
        root_question="What is the total consumption in 2025?",
        resolved_question="What is the total consumption in 2025?",
        last_sql="SELECT SUM(amount) AS total_consumption FROM readings",
        last_columns=columns or ["total_consumption"],
        last_row_count=1,
        turn_count=1,
    )


def test_grain_followup_proposes_monthly_change_preserving_filters() -> None:
    plan = plan_analytics(
        "Plot it per month.",
        viz_intent=parse_intent("Plot it per month.", has_previous=True),
        previous=_previous(),
        db_id="books",
    )

    assert plan.operation == "CHANGE_GRAIN_AND_VISUALIZE"
    assert plan.depends_on_previous is True
    assert plan.requested_analysis.time_grain == "monthly"
    assert plan.semantic_changes[0].operation == "CHANGE_GRAIN"
    assert plan.semantic_changes[0].new_value == "monthly"
    # Nothing in the message touched filters or the metric definition.
    assert plan.preserve_existing["filters"] is True
    assert plan.preserve_existing["metric_definition"] is True
    assert plan.visualization_intent is not None
    assert plan.visualization_intent.requested is True


def test_grouping_change_by_country_names_the_dimension() -> None:
    plan = plan_analytics(
        "Now plot it by country.",
        viz_intent=parse_intent("Now plot it by country.", has_previous=True),
        previous=_previous(columns=["month", "total_consumption"]),
        db_id="books",
    )

    assert plan.operation == "CHANGE_GROUPING_AND_VISUALIZE"
    assert plan.requested_analysis.dimensions == ["country"]


def test_metric_change_is_structured_and_sourced_deterministically() -> None:
    plan = plan_analytics(
        "Instead show average consumption per month.",
        viz_intent=parse_intent("Instead show average consumption per month.", has_previous=True),
        previous=_previous(columns=["month", "consumption"]),
        db_id="books",
    )

    assert plan.requested_analysis.aggregation == "average"
    assert plan.requested_analysis.time_grain == "monthly"
    assert any(
        change.operation == "CHANGE_AGGREGATION" and change.new_value == "average"
        for change in plan.semantic_changes
    )
    assert any(change.operation == "CHANGE_GRAIN" for change in plan.semantic_changes)
    assert plan.source == "deterministic"


def test_filter_addition_preserves_earlier_metric_and_entities() -> None:
    plan = plan_analytics(
        "Now show it only Germany.",
        viz_intent=parse_intent("Now show it only Germany.", has_previous=True),
        previous=_previous(columns=["month", "consumption"]),
        db_id="books",
    )

    assert plan.operation == "ADD_FILTER"
    assert any(change.operation == "ADD_FILTER" for change in plan.semantic_changes)
    assert plan.preserve_existing["metric_definition"] is True


def test_filter_to_value_is_never_treated_as_plain_plot_reuse() -> None:
    plan = plan_analytics(
        "Now filter it to Germany and plot it.",
        viz_intent=parse_intent("Now filter it to Germany and plot it.", has_previous=True),
        previous=_previous(columns=["country", "total_consumption"]), db_id="books",
    )
    assert any(change.operation == "ADD_FILTER" for change in plan.semantic_changes)
    assert plan.preserve_existing["filters"] is False


def test_remove_word_maps_to_remove_filter() -> None:
    plan = plan_analytics(
        "Hide France from this chart.",
        viz_intent=parse_intent("Hide France from this chart.", has_previous=True),
        previous=_previous(columns=["month", "country", "consumption"]),
        db_id="books",
    )

    assert any(change.operation == "REMOVE_FILTER" for change in plan.semantic_changes)


def test_comparison_extends_entities_instead_of_replacing() -> None:
    plan = plan_analytics(
        "Compare with France.",
        viz_intent=parse_intent("Compare with France.", has_previous=True),
        previous=_previous(columns=["month", "country", "consumption"]),
        db_id="books",
    )

    assert plan.operation == "COMPARE"
    compare_changes = [
        change for change in plan.semantic_changes if change.operation == "COMPARE_WITH"
    ]
    assert compare_changes and compare_changes[0].target == "france"
    assert plan.preserve_existing["entities"] is False


def test_plain_plot_request_existing_data_reuses_result() -> None:
    plan = plan_analytics(
        "Plot it.",
        viz_intent=parse_intent("Plot it.", has_previous=True),
        previous=_previous(columns=["month", "total_consumption"]),
        db_id="books",
    )

    assert plan.operation == "VISUALIZE_EXISTING"
    assert plan.depends_on_previous is True


def test_optional_planner_respects_shared_model_call_budget() -> None:
    class ShouldNotCall:
        async def complete(self, *, model: str, prompt: str) -> str:
            raise AssertionError("model call should be denied by request budget")

    draft = plan_analytics(
        "Plot it.", viz_intent=parse_intent("Plot it.", has_previous=True),
        previous=_previous(), db_id="books",
    )
    budget = RequestBudget(10, max_model_calls=0, max_database_calls=0)
    result, _, _ = asyncio.run(refine_plan_with_model(
        ShouldNotCall(), provider="agentrouter", model="test", message="Plot it.",
        previous=_previous(), plan=draft, budget=budget,
    ))
    assert result.source == "fallback"
    assert budget.model_calls == 0


def test_explaining_a_chart_is_not_an_analytical_change() -> None:
    plan = plan_analytics(
        "Why is this chart misleading?",
        viz_intent=parse_intent("Why is this chart misleading?", has_previous=True),
        previous=_previous(columns=["month", "total_consumption"]),
        db_id="books",
    )

    assert plan.operation == "EXPLAIN_VISUALIZATION"
    assert plan.semantic_changes == []


def test_unsupported_chart_family_fails_closed() -> None:
    with pytest.raises(ValueError, match="are not supported"):
        plan_analytics(
            "Make a radar chart of it.",
            viz_intent=parse_intent("Make a radar chart of it.", has_previous=True),
            previous=_previous(),
            db_id="books",
        )


def test_plan_without_chart_or_change_is_unsupported_typed() -> None:
    plan = plan_analytics(
        "Status update?",
        viz_intent=parse_intent("Status update?", has_previous=False),
        previous=None,
        db_id="books",
    )
    assert plan.requested_analysis.metrics == []


def test_plan_contract_rejects_unknown_fields_and_bad_literals() -> None:
    with pytest.raises(ValidationError):
        AnalyticsPlan(
            operation="VISUALIZE_EXISTING",
            requested_analysis={"time_grain": "fortnightly"},
        )
    with pytest.raises(ValidationError):
        AnalyticsPlan.model_validate(
            {"operation": "VISUALIZE", "depends_on_previous": True, "surprise": True}
        )


def test_planner_feature_flag_defaults_on_and_can_be_disabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("TEXT2SQL_ANALYTICS_PLANNER_ENABLED", raising=False)
    assert planner_enabled() is True
    monkeypatch.setenv("TEXT2SQL_ANALYTICS_PLANNER_ENABLED", "false")
    assert planner_enabled() is False


class _RefiningCompleter:
    """Returns a strict model plan proposal; records the prompt it received."""

    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.prompts: list[str] = []

    async def complete_detailed(self, model: str, prompt: str) -> tuple[str, object]:
        self.prompts.append(prompt)
        return json.dumps(self.payload if not isinstance(self.payload, str) else self.payload), None


def test_model_refinement_is_validated_into_the_strict_plan() -> None:
    previous = _previous(columns=["month", "consumption", "country"])
    deterministic = plan_analytics(
        "Plot it per month for France.",
        viz_intent=parse_intent("Plot it per month for France.", has_previous=True),
        previous=previous,
        db_id="books",
    )
    completer = _RefiningCompleter(
        {
            "operation": "CHANGE_GRAIN_AND_VISUALIZE",
            "depends_on_previous": True,
            "requested_analysis": {"time_grain": "monthly"},
            "semantic_changes": [
                {"operation": "CHANGE_GRAIN", "target": "time_grain", "new_value": "monthly"},
                {"operation": "ADD_FILTER", "target": "France"},
            ],
            "visualization_intent": {"requested": True},
        }
    )

    refined, _usage, latency = asyncio.run(
        refine_plan_with_model(
            completer,
            provider="groq",
            model="mocked-model",
            message="Plot it per month for France.",
            previous=previous,
            plan=deterministic,
        )
    )

    assert refined.operation == "CHANGE_GRAIN_AND_VISUALIZE"
    assert refined.requested_analysis.time_grain == "monthly"
    assert any(change.operation == "ADD_FILTER" for change in refined.semantic_changes)
    assert refined.source == "model"
    assert latency >= 0
    assert completer.prompts and "Do not generate SQL" in completer.prompts[0]


def test_invalid_model_refinement_falls_back_to_the_deterministic_plan() -> None:
    previous = _previous(columns=["month", "consumption"])
    deterministic = plan_analytics(
        "Plot it.",
        viz_intent=parse_intent("Plot it.", has_previous=True),
        previous=previous,
        db_id="books",
    )
    complement = _RefiningCompleter('{"operation": "MAGIC_ORDER", "surprise": true}')

    refined, _usage, _latency = asyncio.run(
        refine_plan_with_model(
            complement,
            provider="groq",
            model="mocked-model",
            message="Plot it.",
            previous=previous,
            plan=deterministic,
        )
    )

    assert refined.source == "fallback"
    assert refined.operation == deterministic.operation


async def _run_failing_refinement(
    deterministic: AnalyticsPlan,
) -> tuple[AnalyticsPlan, object, int]:
    class _FailingCompleter:
        async def complete(self, model: str, prompt: str) -> str:
            raise TimeoutError("provider timeout")

    return await refine_plan_with_model(
        _FailingCompleter(),
        provider="groq",
        model="mocked-model",
        message="Plot it.",
        previous=_previous(),
        plan=deterministic,
    )


def test_timeout_or_transport_failure_falls_back_cleanly() -> None:
    previous = _previous()
    deterministic = plan_analytics(
        "Plot it.",
        viz_intent=parse_intent("Plot it.", has_previous=True),
        previous=previous,
        db_id="books",
    )

    refined, _usage, _latency = asyncio.run(_run_failing_refinement(deterministic))

    assert refined.source == "fallback"
    assert refined.operation == deterministic.operation


def test_model_refinement_prefers_task_aware_json_mode() -> None:
    class _TaskCompleter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str, bool]] = []

        async def complete_task(
            self, model: str, prompt: str, task: str, *, json_mode: bool = False,
        ) -> tuple[str, TokenUsage]:
            self.calls.append((model, prompt, task, json_mode))
            return (
                json.dumps({
                    "operation": "CHANGE_GRAIN_AND_VISUALIZE",
                    "depends_on_previous": True,
                    "requested_analysis": {"time_grain": "monthly"},
                    "semantic_changes": [
                        {
                            "operation": "CHANGE_GRAIN",
                            "target": "time_grain",
                            "new_value": "monthly",
                        },
                    ],
                    "visualization_intent": {"requested": True},
                }),
                TokenUsage(input_tokens=7, output_tokens=5),
            )

        async def complete_detailed(self, model: str, prompt: str) -> tuple[str, TokenUsage]:
            raise AssertionError("complete_task should take precedence")

    previous = _previous(columns=["month", "consumption"])
    deterministic = plan_analytics(
        "Plot it per month.",
        viz_intent=parse_intent("Plot it per month.", has_previous=True),
        previous=previous,
        db_id="books",
    )
    completer = _TaskCompleter()

    refined, usage, _latency = asyncio.run(
        refine_plan_with_model(
            completer,
            provider="groq",
            model="mocked-model",
            message="Plot it per month.",
            previous=previous,
            plan=deterministic,
        )
    )

    assert refined.source == "model"
    assert refined.requested_analysis.time_grain == "monthly"
    assert usage.input_tokens == 7
    assert completer.calls == [
        ("mocked-model", completer.calls[0][1], "decision", True),
    ]
    assert "Plot it per month." in completer.calls[0][1]
