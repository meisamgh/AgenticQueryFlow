"""Regression for metric names followed by temporal chart instructions."""

from semantic_text2sql.analytics_planner import plan_analytics
from semantic_text2sql.data_requirement_validator import validate_reusability
from semantic_text2sql.visualization import parse_intent


def test_total_consumption_over_date_and_plot_it_reuses_matching_result() -> None:
    question = "total consumption over date and plot it"
    plan = plan_analytics(
        question,
        viz_intent=parse_intent(question),
        previous=None,
        db_id="consumption",
    )
    assert plan.requested_analysis.metrics == ["consumption"]
    grounded = plan.model_copy(update={
        "semantic_changes": [],
        "requested_analysis": plan.requested_analysis.model_copy(update={
            "metrics": ["total_consumption"],
        }),
    })
    decision = validate_reusability(
        grounded,
        stored_columns=["month", "total_consumption"],
        stored_rows=[["202401", 15.0], ["202402", 20.0]],
        stored_row_count=2,
        stored_truncated=False,
    )
    assert decision.decision == "reuse_result"
