"""Deterministic, ranked chart recommendations over actual executed results.

Every candidate is validated through :func:`visualization.plan_chart` against
the real rows before it is recommended, so a recommendation can never describe
a chart the executed data does not support. Recommendations never call the
database, never invent columns or values, and never request derived measures
without a verified definition — those belong to the text-to-SQL pipeline.
"""

from __future__ import annotations

import re
from typing import Any

from semantic_text2sql.models import (
    ChartRecommendations as ChartRecommendations,
)
from semantic_text2sql.models import (
    ChartSpec,
    ChartType,
)
from semantic_text2sql.models import (
    RecommendedChart as RecommendedChart,
)
from semantic_text2sql.visualization import (
    VisualizationError,
    VisualizationIntent,
    _column_roles,
    measure_unit,
    plan_chart,
)

_TIME_WORDS = re.compile(r"\bover\s+time\b|\btrend\b|\bgrowth\b", re.I)
_COMPARE_WORDS = re.compile(r"\bcompare\b|\bversus\b|\bvs\.?\b", re.I)
_DISTRIBUTION_WORDS = re.compile(r"\bdistribution\b|\bhistogram\b|\bspread\b|\bmedian\b", re.I)
_CORRELATION_WORDS = re.compile(r"\bcorrelation\b|\brelationship\b|\bagainst\b|\bscatter\b", re.I)
_SHARE_WORDS = re.compile(r"\bshare\b|\bproportion\b|\bcomposition\b", re.I)
_IDENTIFIER_NAME = re.compile(
    r"(?:^|_)(?:id|uid|guid|code|uuid|rank|row|num|no|number|year)(?:$|_)", re.I
)


def _mentioned_measures(names: list[str], question: str) -> int:
    return sum(
        1 for name in names if re.search(rf"\b{re.escape(name)}\b", question, re.I)
    )

_TYPE_BASE: dict[ChartType, float] = {
    "line": 0.85,
    "multi_line": 0.8,
    "area": 0.7,
    "stacked_area": 0.65,
    "step_line": 0.55,
    "bar": 0.8,
    "horizontal_bar": 0.8,
    "grouped_bar": 0.75,
    "stacked_bar": 0.6,
    "percent_stacked_bar": 0.55,
    "scatter": 0.7,
    "bubble": 0.6,
    "histogram": 0.6,
    "box": 0.6,
    "heatmap": 0.6,
    "pie": 0.5,
    "donut": 0.55,
    "treemap": 0.5,
    "calendar_heatmap": 0.5,
}


def _cardinality(column: str, columns: list[str], rows: list[dict[str, Any]]) -> int:
    return len({row[column] for row in rows})


def _identifier_like(name: str, values: list[Any]) -> bool:
    if _IDENTIFIER_NAME.search(name):
        return True
    numeric_values = [int(value) for value in values if isinstance(value, (int, float))]
    if len(numeric_values) == len(values) and len(numeric_values) > 2:
        distinct = sorted(set(numeric_values))
        return (
            len(distinct) == len(numeric_values)
            and distinct[-1] - distinct[0] == len(distinct) - 1
        )
    return False


def _nonnegative(rows: list[dict[str, Any]], column: str) -> bool:
    return all(
        float(row[column]) >= 0
        for row in rows
        if isinstance(row[column], (int, float)) and not isinstance(row[column], bool)
    )


def _measure_clusters(numeric: list[str]) -> dict[str | None, list[str]]:
    clusters: dict[str | None, list[str]] = {}
    for name in numeric:
        clusters.setdefault(measure_unit(name), []).append(name)
    return clusters


def _intent_for(candidate: RecommendedChart) -> dict[str, Any]:
    return {
        "requested": True,
        "chart_type": candidate.chart_type,
        "x": candidate.x,
        "y": candidate.y,
        "series": candidate.series,
        "y_measures": candidate.y_measures,
    }


def _mentioned(columns: list[str], text: str) -> str | None:
    matches = [
        name
        for name in columns
        if re.search(
            rf"(?<!\w){re.escape(name.casefold().replace('_', ' '))}(?!\w)",
            " ".join(text.casefold().split()).replace("_", " "),
        )
    ]
    return max(matches, key=len) if matches else None


def recommend_charts(
    question: str,
    columns: list[str],
    rows: list[list[Any]],
    *,
    truncated: bool = False,
) -> ChartRecommendations | None:
    """Rank validated chart candidates for an executed, complete result.

    ``None`` means no faithful chart exists for this data; never fall back to
    a fabricated chart shape.
    """
    if truncated or not rows or len(columns) < 2:
        return None
    if any(len(row) != len(columns) for row in rows):
        return None
    named_rows = [dict(zip(columns, row, strict=True)) for row in rows]
    numeric, temporal, category = _column_roles(columns, rows)
    mentioned = _mentioned(columns, question)
    measures = [
        name
        for name in numeric
        if not _identifier_like(name, [row[columns.index(name)] for row in rows])
    ]
    usable = measures or numeric
    if not usable:
        return None

    candidates: list[tuple[float, RecommendedChart]] = []
    mention_boost = 0.25 * (mentioned in usable)

    def primary_measure(preferred: str | None = None) -> str | None:
        pool = [name for name in usable if name == preferred] or usable
        if preferred and preferred in pool:
            return preferred
        totals = {
            name: sum(
                float(row[columns.index(name)])
                for row in rows
                if isinstance(row[columns.index(name)], (int, float))
            )
            for name in pool
        }
        return max(pool, key=lambda name: totals[name])

    def add_candidate(
        chart_type: ChartType,
        x: str | None,
        y: str | None,
        *,
        series: str | None = None,
        y_measures: list[str] | None = None,
        boost: float = 0.0,
        reason: str = "",
    ) -> None:
        if y is None or (x is None and chart_type not in {"histogram", "box"}):
            return

        def extra_score() -> float:
            score = _TYPE_BASE[chart_type] + boost
            if mention_boost and y == mentioned:
                score += mention_boost
            return score

        candidates.append(
            (
                extra_score(),
                RecommendedChart(
                    chart_type=chart_type,
                    x=x or y,
                    y=y,
                    series=series,
                    y_measures=list(y_measures or []),
                    title="",
                    reason=reason,
                    score=0.0,
                ),
            )
        )

    if temporal:
        time_axis = temporal[0]
        clusters = _measure_clusters(usable)
        wide_family = max(
            (members for members in clusters.values() if 2 <= len(members) <= 4),
            key=len,
            default=None,
        )
        primary = primary_measure(mentioned)
        measure_boost = (
            0.35
            if wide_family and _mentioned_measures(wide_family, question) >= 2
            else 0.1
        )
        if wide_family and primary in wide_family:
            add_candidate(
                "multi_line",
                time_axis,
                primary,
                y_measures=wide_family,
                boost=measure_boost + (0.1 if _TIME_WORDS.search(question) else 0.0),
                reason=f"{', '.join(wide_family[:-1])} and {wide_family[-1]} share comparable "
                "units along the time axis",
            )
            add_candidate(
                "grouped_bar",
                time_axis,
                primary,
                y_measures=wide_family,
                boost=measure_boost,
                reason="grouped bars compare the compatible measures side by side per period",
            )
        assert primary is not None
        add_candidate(
            "line",
            time_axis,
            primary,
            boost=0.15 if _TIME_WORDS.search(question) else 0.0,
            reason="a trend line shows how the measure evolves over the time axis",
        )
        if _nonnegative(named_rows, primary):
            add_candidate(
                "area",
                time_axis,
                primary,
                reason="a filled area emphasizes the cumulative trend shape",
            )
        if category:
            top_category = category[0]
            series_cardinality = _cardinality(top_category, columns, named_rows)
            if series_cardinality <= 20:
                add_candidate(
                    "multi_line",
                    time_axis,
                    primary,
                    series=top_category,
                    boost=0.2 if _COMPARE_WORDS.search(question) else 0.1,
                    reason=f"one line per {top_category.replace('_', ' ')} supports comparison",
                )
                add_candidate(
                    "grouped_bar",
                    time_axis,
                    primary,
                    series=top_category,
                    boost=0.1,
                    reason=
                    "grouped bars isolate each "
                    f"{top_category.replace('_', ' ')} within every period",
                )
                if (
                    _nonnegative(named_rows, primary)
                    and measure_unit(primary) not in {"ratio", None}
                ):
                    add_candidate(
                        "stacked_area",
                        time_axis,
                        primary,
                        series=top_category,
                        reason=
                        f"stacking additive contributions per {top_category.replace('_', ' ')} "
                        "keeps the total meaningful",
                    )
    elif category and len(usable) >= 1:
        axis = category[0]
        cardinality = _cardinality(axis, columns, named_rows)
        axis_boost = 0.2 if re.search(rf"\b{re.escape(axis)}\b", question, re.I) else 0.1
        primary = primary_measure(mentioned)
        assert primary is not None
        add_candidate(
            "horizontal_bar" if cardinality > 8 else "bar",
            axis,
            primary,
            boost=axis_boost,
            reason=f"a bar chart compares the measure across {cardinality} "
            f"{axis.replace('_', ' ')} values",
        )
        clusters = _measure_clusters(usable)
        wide_family = max(
            (members for members in clusters.values() if 2 <= len(members) <= 4),
            key=len,
            default=None,
        )
        measure_boost = (
            0.35
            if wide_family and _mentioned_measures(wide_family, question) >= 2
            else 0.1
        )
        if wide_family and primary in wide_family:
            add_candidate(
                "multi_line",
                axis,
                primary,
                y_measures=wide_family,
                boost=measure_boost,
                reason=f"{', '.join(wide_family[:-1])} and {wide_family[-1]} are comparable "
                "and readable as parallel lines",
            )
            add_candidate(
                "grouped_bar",
                axis,
                primary,
                y_measures=wide_family,
                boost=measure_boost,
                reason="grouped bars compare the compatible measures side by side",
            )
        donut_ok = (
            cardinality <= 6
            and _nonnegative(named_rows, primary)
            and measure_unit(primary) != "ratio"
        )
        if donut_ok:
            add_candidate(
                "donut",
                axis,
                primary,
                boost=0.25 if _SHARE_WORDS.search(question) else 0.0,
                reason="a donut shows each category's share of the small whole",
            )
        if 6 < cardinality <= 60:
            add_candidate(
                "treemap",
                axis,
                primary,
                reason="a treemap keeps a moderate category set readable",
            )
        if len(category) >= 2:
            second = next(name for name in category if name != axis)
            add_candidate(
                "grouped_bar",
                axis,
                primary,
                series=second,
                reason="grouped bars isolate the second dimension within each category",
            )
            if cardinality * _cardinality(second, columns, named_rows) <= 200:
                add_candidate(
                    "heatmap",
                    axis,
                    primary,
                    series=second,
                    reason="a heatmap reads dense two-dimensional category combinations",
                )
    elif len(usable) >= 2:
        x_measure, y_measure = usable[0], usable[1]
        if mentioned and mentioned != x_measure and mentioned in usable:
            y_measure = mentioned
        boost = 0.25 if _CORRELATION_WORDS.search(question) else 0.0
        add_candidate(
            "scatter",
            x_measure,
            y_measure,
            boost=boost,
            reason="a scatter plot exposes the relationship between two measures",
        )
        remaining = [name for name in usable if name not in {x_measure, y_measure}]
        if remaining:
            add_candidate(
                "bubble",
                x_measure,
                y_measure,
                boost=0.15,
                reason="a bubble chart adds a third magnitude through bubble size",
            )
    elif len(rows) >= 5:
        boost = 0.25 if _DISTRIBUTION_WORDS.search(question) else 0.05
        add_candidate(
            "histogram",
            None,
            usable[0],
            boost=boost,
            reason="an observation-distribution chart is the only faithful view",
        )

    ranked: list[RecommendedChart] = []
    seen: set[tuple[Any, ...]] = set()
    for score, candidate in sorted(candidates, key=lambda item: item[0], reverse=True):
        key = (
            candidate.chart_type,
            candidate.x,
            candidate.y,
            candidate.series,
            tuple(candidate.y_measures),
        )
        if key in seen:
            continue
        try:
            spec = plan_chart(
                VisualizationIntent(**_intent_for(candidate)),
                question=question,
                columns=columns,
                rows=rows,
                truncated=truncated,
            )
        except VisualizationError:
            seen.add(key)
            continue
        seen.add(key)
        ranked.append(
            candidate.model_copy(
                update={"title": spec.title, "score": round(score - 0.15 * len(spec.warnings), 3)}
            )
        )
    if not ranked:
        return None
    primary_rec = ranked[0]
    alternatives = [item for item in ranked[1:] if item.score >= 0.3][:3]
    return ChartRecommendations(primary_chart=primary_rec, alternative_charts=alternatives)


def build_recommendation_intent(recommendation: RecommendedChart) -> VisualizationIntent:
    return VisualizationIntent(**_intent_for(recommendation))


def materialize_recommendation(
    recommendation: RecommendedChart,
    *,
    question: str,
    columns: list[str],
    rows: list[list[Any]],
    truncated: bool,
) -> ChartSpec:
    """Convert a recommendation into the validated chart contract."""
    return plan_chart(
        build_recommendation_intent(recommendation),
        question=question,
        columns=columns,
        rows=rows,
        truncated=truncated,
    )
