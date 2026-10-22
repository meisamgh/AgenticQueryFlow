"""Run the Scenario A-J acceptance matrix for the v6 feature work.

Each scenario maps to already-committed pytest node ids, so the suite stays a
thin, auditable selector over the real behavior tests — no duplicated logic.
Usage:  .venv/bin/python scripts/evaluate_v6_scenarios.py
Exit code 0 when every scenario passes.
"""

from __future__ import annotations

import subprocess
import sys

ANALYTICS_API = "tests/test_analytics_api.py"
DATA_REQUIREMENTS = "tests/test_data_requirement_validator.py"
RECOMMENDER = "tests/test_visualization_recommender.py"
FILTERS = "tests/test_filter_discovery.py"
FILTER_API = "tests/test_filter_api.py"

SCENARIOS: dict[str, list[str]] = {
    "A": [
        ANALYTICS_API + "::test_scenario_a_scalar_total_per_month_routes_through_validated_new_sql",
    ],
    "B": [
        ANALYTICS_API + "::test_scenario_b_complete_monthly_result_reused_without_new_sql",
    ],
    "C": [
        DATA_REQUIREMENTS + "::test_scenario_c_missing_country_dimension_requeries_and_preserves_state",  # noqa: E501
    ],
    "D": [
        DATA_REQUIREMENTS
        + "::test_scenario_d_missing_dimension_for_non_additive_metric_notes_additivity",
        RECOMMENDER + "::test_scenario_d_eight_column_result_recommends_compatible_charts",
    ],
    "E": [
        FILTER_API + "::test_chat_response_carries_recommendations_and_filters",
        FILTER_API + "::test_measures_never_appear_as_client_filters",
    ],
    "F": [
        RECOMMENDER + "::test_scenario_f_compatible_measures_chart_together",
    ],
    "G": [
        FILTERS + "::test_sensitive_column_becomes_unavailable_never_proposed",
        FILTERS + "::test_high_cardinality_text_column_is_unavailable",
        FILTERS + "::test_identifier_and_primary_key_columns_excluded",
    ],
    "H": [
        RECOMMENDER + "::test_scenario_h_category_comparison_and_alternatives",
        RECOMMENDER + "::test_scenario_h_time_series_variants_rank_primary_line",
        RECOMMENDER + "::test_scenario_h_correlation_prefers_scatter",
        RECOMMENDER + "::test_scenario_h_multi_dimension_result_uses_series",
        RECOMMENDER + "::test_scenario_h_distribution_recommends_histogram_when_no_categories",
    ],
    "I": [
        FILTER_API + "::test_filter_values_returns_bounded_distinct_values",
        FILTER_API + "::test_filter_values_rejects_unsafe_fields",
    ],
    "J": [
        FILTERS + "::test_truncated_result_demotes_client_filters",
    ],
}


def main() -> int:
    failed: list[str] = []
    for scenario in sorted(SCENARIOS):
        node_ids = SCENARIOS[scenario]
        result = subprocess.run(
            [sys.executable, "-m", "pytest", *node_ids, "-q", "-p", "no:cacheprovider"],
            capture_output=True,
            text=True,
        )
        status = "PASS" if result.returncode == 0 else "FAIL"
        if result.returncode != 0:
            failed.append(scenario)
        print(f"Scenario {scenario}: {status} ({len(node_ids)} checks)")
        if result.returncode != 0:
            print(result.stdout[-2_000:])
            print(result.stderr[-500:])
    if failed:
        print(f"\nFAILED scenarios: {', '.join(failed)}")
        return 1
    print("\nAll scenarios pass.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
