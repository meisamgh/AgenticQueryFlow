"""Bounded plotting specialist over complete accepted query results.

The model proposes chart design only. Existing chart planning validates axes,
row identity, chart shape, and truncation before any proposal reaches Plotly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, cast

from semantic_text2sql.analytics_planner import plan_analytics
from semantic_text2sql.data_requirement_validator import validate_reusability
from semantic_text2sql.models import (
    CallLedgerEntry,
    ChartRecommendations,
    ChartSpec,
    ChatResponse,
    ConversationState,
    RequestedChange,
    TokenUsage,
)
from semantic_text2sql.result_cache import RecentResult
from semantic_text2sql.runtime import RequestBudget
from semantic_text2sql.visualization import VisualizationError, parse_intent, plan_chart
from semantic_text2sql.visualization_recommender import (
    materialize_recommendation,
    recommend_charts,
    refine_charts_with_model,
)

PlotStatus = Literal[
    "CHART_READY", "REQUERY_REQUIRED", "CLARIFICATION_REQUIRED", "UNSUPPORTED"
]


@dataclass(frozen=True)
class PlottingOutcome:
    status: PlotStatus
    reason: str
    spec: ChartSpec | None = None
    recommendations: ChartRecommendations | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    ledger: tuple[CallLedgerEntry, ...] = ()


async def design_chart(
    *,
    result: ChatResponse | RecentResult | None,
    objective: str,
    completer: Any,
    provider: str,
    model: str,
    budget: RequestBudget,
    previous: ConversationState | None = None,
    db_id: str | None = None,
    fresh_result: bool = False,
    changes: list[RequestedChange] | None = None,
) -> PlottingOutcome:
    """Design from the exact accepted rows; never rerun SQL or invent values."""
    generation = result.generation if result is not None else None
    if generation is None or not generation.accepted:
        return PlottingOutcome("REQUERY_REQUIRED", "No accepted result is available to plot.")
    if generation.truncated or generation.row_count != len(generation.rows):
        return PlottingOutcome(
            "REQUERY_REQUIRED", "The accepted result is incomplete; a new governed query is needed."
        )
    selected_dimensions = [
        str(item.new_value or item.target) for item in changes or []
        if item.operation == "ADD_DIMENSION"
    ]
    stored_names = {column.casefold() for column in generation.columns}
    if any(name.casefold() not in stored_names for name in selected_dimensions):
        return PlottingOutcome(
            "REQUERY_REQUIRED",
            "The executed result lacks the requested grouping column; new SQL is required.",
        )
    if not fresh_result:
        analytical_changes = {
            "ADD_FILTER", "REMOVE_FILTER", "REPLACE_FILTER", "CHANGE_METRIC",
            "CHANGE_AGGREGATION", "CHANGE_GRAIN", "CHANGE_PERIOD", "COMPARE",
            "CHANGE_RANKING", "REMOVE_DIMENSION",
        }
        if any(item.operation in analytical_changes for item in changes or []):
            return PlottingOutcome(
                "REQUERY_REQUIRED",
                "This request changes the underlying analysis; new validated SQL is required.",
            )
    visualization_intent = parse_intent(objective, has_previous=True)
    if not visualization_intent.requested:
        visualization_intent = visualization_intent.model_copy(update={"requested": True})
    plan = plan_analytics(
        objective,
        viz_intent=visualization_intent,
        previous=None if fresh_result else previous,
        db_id=db_id or generation.db_id,
    )
    if (
        not fresh_result
        and previous is not None
        and visualization_intent.explicit_type
        and not changes
        and previous.resolved_question
    ):
        # A chart-type request may repeat the existing metric and grouping in
        # ordinary language. The lexical planner reports those mentions as
        # changes, although the accepted rows already contain that analysis.
        # Discard only provably identical analytical requirements; novel ones
        # still pass through the result-reusability gate and require SQL.
        baseline = plan_analytics(
            previous.resolved_question,
            viz_intent=parse_intent(previous.resolved_question, has_previous=False),
            previous=None,
            db_id=db_id or generation.db_id,
        )
        requested = plan.requested_analysis
        original = baseline.requested_analysis
        if (
            requested.metrics == original.metrics
            and requested.dimensions == original.dimensions
            and requested.time_grain == original.time_grain
            and requested.aggregation == original.aggregation
            and requested.derived_measures == original.derived_measures
            and all(change.operation in {
                "CHANGE_METRIC", "CHANGE_AGGREGATION", "ADD_DIMENSION"
            } for change in plan.semantic_changes)
        ):
            aliases = {name.casefold(): name for name in generation.columns}
            mapped_metrics = [
                aliases.get(metric.casefold())
                or next((name for name in generation.columns
                         if name.casefold() in {
                             f"{prefix}_{metric.casefold()}"
                             for prefix in ("total", "sum", "avg", "average", "count")
                         }), metric)
                for metric in requested.metrics
            ]
            plan = plan.model_copy(update={
                "semantic_changes": [],
                "requested_analysis": requested.model_copy(update={"metrics": mapped_metrics}),
            })
    if fresh_result:
        # A newly executed result has no *previous* analysis to change. Still
        # check its actual columns and grain before giving it to the chart model.
        aliases = {name.casefold(): name for name in generation.columns}
        mapped_metrics = [
            aliases.get(metric.casefold())
            or next((name for name in generation.columns
                     if name.casefold() in {
                         f"{prefix}_{metric.casefold()}"
                         for prefix in ("total", "sum", "avg", "average", "count")
                     }), metric)
            for metric in plan.requested_analysis.metrics
        ]
        plan = plan.model_copy(update={
            "semantic_changes": [],
            "requested_analysis": plan.requested_analysis.model_copy(
                update={"metrics": mapped_metrics}
            ),
        })
    requirement = validate_reusability(
        plan,
        stored_columns=generation.columns,
        stored_rows=generation.rows,
        stored_row_count=generation.row_count,
        stored_truncated=generation.truncated,
    )
    if requirement.decision != "reuse_result":
        status = cast(PlotStatus, {
            "requery_database": "REQUERY_REQUIRED",
            "need_clarification": "CLARIFICATION_REQUIRED",
            "unsupported": "UNSUPPORTED",
        }[requirement.decision])
        return PlottingOutcome(status, requirement.reason)
    if not generation.rows or len(generation.columns) < 2:
        return PlottingOutcome(
            "UNSUPPORTED", "This result does not contain enough rows and columns for a chart."
        )
    prior = result.presentation.chart_spec if result is not None and result.presentation else None
    intent = parse_intent(objective, has_previous=True)
    if not intent.requested:
        intent = intent.model_copy(update={"requested": True})
    if prior is not None and objective.strip().casefold() in {
        "plot", "plot it", "show the chart", "show it again", "resize", "zoom",
    }:
        return PlottingOutcome("CHART_READY", "Reused the validated chart.", spec=prior)

    # The dedicated model sees a bounded profile/sample, not the full result;
    # its proposal is always rechecked against every executed row.
    calls_before = budget.model_calls
    recommendations, usage, latency_ms = await refine_charts_with_model(
        completer,
        provider=provider,
        model=model,
        question=objective,
        columns=generation.columns,
        rows=generation.rows,
        truncated=False,
        budget=budget,
        required_chart_type=intent.chart_type if intent.explicit_type else None,
    )
    model_called = budget.model_calls > calls_before
    ledger = (
        CallLedgerEntry(
            component="plotting_model",
            kind="MODEL",
            status="SUCCEEDED" if recommendations is not None else "FAILED",
            provider=provider,
            requested_model=model,
            effective_model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            latency_ms=latency_ms,
        ),
    ) if model_called else ()
    if recommendations is not None:
        try:
            spec = materialize_recommendation(
                recommendations.primary_chart,
                question=objective,
                columns=generation.columns,
                rows=generation.rows,
                truncated=False,
            )
            return PlottingOutcome(
                "CHART_READY", "The plotting model proposed a validated chart.",
                spec=spec, recommendations=recommendations, usage=usage, ledger=ledger,
            )
        except VisualizationError:
            pass
    # A failed model proposal cannot enter the client. A deterministic chart
    # may still be possible, and is explicitly identified as a fallback.
    try:
        spec = plan_chart(
            intent,
            question=objective,
            columns=generation.columns,
            rows=generation.rows,
            truncated=False,
        )
    except VisualizationError as exc:
        return PlottingOutcome("UNSUPPORTED", str(exc), usage=usage, ledger=ledger)
    return PlottingOutcome(
        "CHART_READY", "Used a validated chart after the plotting model was unavailable.",
        spec=spec, recommendations=recommend_charts(
            objective, generation.columns, generation.rows, truncated=False
        ), usage=usage, ledger=ledger,
    )
