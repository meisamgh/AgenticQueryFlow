"""Numeric candidate selection must not change fractional observations."""
from semantic_text2sql.visualization_recommender import _identifier_like, recommend_charts


def test_fractional_measure_is_not_a_sequential_identifier() -> None:
    assert not _identifier_like("consumption", [.1, 1.2, 2.3])


def test_requested_fractional_measure_remains_a_chart_candidate() -> None:
    result = recommend_charts(
        "Plot consumption by month", ["month", "consumption", "cost"],
        [["202501", .1, 10.0], ["202502", 1.2, 20.0], ["202503", 2.3, 30.0]],
    )
    assert result is not None
    assert result.primary_chart.y == "consumption"
