"""Scenario tests for the deterministic result-reusability gate."""

from __future__ import annotations

from semantic_text2sql.analytics_planner import (
    AnalyticsPlan,
    RequestedAnalysis,
    SemanticChange,
)
from semantic_text2sql.data_requirement_validator import validate_reusability
from semantic_text2sql.visualization import parse_intent


def _plan(
    *,
    metrics: list[str] | None = None,
    dimensions: list[str] | None = None,
    grain: str | None = None,
    chart: bool = True,
) -> AnalyticsPlan:
    return AnalyticsPlan(
        operation="VISUALIZE_EXISTING",
        depends_on_previous=True,
        requested_analysis=RequestedAnalysis(
            metrics=metrics or [],
            dimensions=dimensions or [],
            time_grain=grain,  # type: ignore[arg-type]
        ),
        semantic_changes=[SemanticChange(operation="CHANGE_GRAIN", target="time_grain")]
        if grain
        else [],
        visualization_intent=parse_intent("Plot it", has_previous=True)
        if chart
        else None,
    )


def test_value_scenario_a_scalar_total_asked_per_month_requeries() -> None:
    requirement = validate_reusability(
        _plan(grain="monthly"),
        stored_columns=["total_consumption"],
        stored_rows=[[125_000]],
        stored_row_count=1,
        stored_truncated=False,
    )

    assert requirement.decision == "requery_database"
    assert "cannot be inferred" in requirement.reason or "coarser" in requirement.reason
    assert requirement.requested_grain == "monthly"


def test_scenario_b_complete_monthly_rows_are_reused_without_sql() -> None:
    requirement = validate_reusability(
        _plan(),
        stored_columns=["month", "total_consumption"],
        stored_rows=[["202501", 1000], ["202502", 1200], ["202503", 1400]],
        stored_row_count=3,
        stored_truncated=False,
    )

    assert requirement.decision == "reuse_result"
    assert not requirement.additivity_note


def test_metric_and_filter_changes_require_new_sql() -> None:
    for operation in (
        "ADD_FILTER", "REMOVE_FILTER", "REPLACE_FILTER", "CHANGE_METRIC", "COMPARE_WITH"
    ):
        plan = _plan()
        plan.semantic_changes = [SemanticChange(operation=operation, target="country")]
        result = validate_reusability(
            plan, stored_columns=["month", "total_consumption"],
            stored_rows=[["202501", 100]], stored_row_count=1,
            stored_truncated=False,
        )
        assert result.decision == "requery_database", operation


def test_missing_requested_metric_cannot_reuse_aggregate() -> None:
    result = validate_reusability(
        _plan(metrics=["profit"]),
        stored_columns=["month", "revenue"],
        stored_rows=[["202501", 100]], stored_row_count=1,
        stored_truncated=False,
    )
    assert result.decision == "requery_database"


def test_incomplete_saved_snapshot_cannot_be_reused() -> None:
    result = validate_reusability(
        _plan(), stored_columns=["month", "revenue"],
        stored_rows=[["202501", 100]], stored_row_count=2,
        stored_truncated=False,
    )
    assert result.decision == "need_clarification"


def test_scenario_c_missing_country_dimension_requeries_and_preserves_state() -> None:
    requirement = validate_reusability(
        _plan(dimensions=["country"]),
        stored_columns=["month", "total_consumption"],
        stored_rows=[["202501", 1000], ["202502", 1200]],
        stored_row_count=2,
        stored_truncated=False,
    )

    assert requirement.decision == "requery_database"
    assert "country" in requirement.reason


def test_scenario_d_missing_dimension_for_non_additive_metric_notes_additivity(
) -> None:
    requirement = validate_reusability(
        _plan(dimensions=["country"], metrics=["conversion_rate"]),
        stored_columns=["month", "conversion_rate"],
        stored_rows=[["202501", 0.1], ["202502", 0.3]],
        stored_row_count=2,
        stored_truncated=False,
    )

    assert requirement.decision == "requery_database"
    assert requirement.additivity_note is not None
    assert "non-additive" in requirement.additivity_note


def test_truncated_results_never_reuse() -> None:
    requirement = validate_reusability(
        _plan(),
        stored_columns=["month", "usage"],
        stored_rows=[["202501", 1], ["202502", 2]],
        stored_row_count=10,
        stored_truncated=True,
    )

    assert requirement.decision == "need_clarification"


def test_empty_results_cannot_be_visualized() -> None:
    requirement = validate_reusability(
        _plan(),
        stored_columns=["month", "usage"],
        stored_rows=[],
        stored_row_count=0,
        stored_truncated=False,
    )

    assert requirement.decision == "need_clarification"


def test_unavailable_stored_result_asks_for_the_original_question_again() -> None:
    requirement = validate_reusability(
        _plan(),
        stored_columns=None,
        stored_rows=None,
        stored_row_count=None,
        stored_truncated=False,
    )

    assert requirement.decision == "need_clarification"
    assert "Ask the original question again" in requirement.reason


def test_finishing_grain_change_from_monthly_to_yearly_requeries_for_sql_grain() -> None:
    requirement = validate_reusability(
        _plan(grain="yearly"),
        stored_columns=["month", "usage"],
        stored_rows=[["202501", 10]] * 6,
        stored_row_count=6,
        stored_truncated=False,
    )

    assert requirement.decision == "requery_database"


def test_non_additive_metrics_cannot_be_re_grouped_client_side() -> None:
    requirement = validate_reusability(
        _plan(grain="yearly", metrics=["average_consumption"]),
        stored_columns=["month", "average_consumption"],
        stored_rows=[["202501", 10], ["202502", 20]],
        stored_row_count=2,
        stored_truncated=False,
    )

    assert requirement.decision == "requery_database"
    assert requirement.additivity_note and "average_consumption" in requirement.additivity_note


def test_no_extractable_analysis_is_unsupported() -> None:
    plan = AnalyticsPlan(
        operation="EXPLAIN_VISUALIZATION",
        depends_on_previous=False,
        visualization_intent=None,
    )

    requirement = validate_reusability(
        plan,
        stored_columns=["x"],
        stored_rows=[[1]],
        stored_row_count=1,
        stored_truncated=False,
    )

    assert requirement.decision == "unsupported"
