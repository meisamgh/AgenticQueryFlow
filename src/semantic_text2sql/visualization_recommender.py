"""Deterministic, ranked chart recommendations over actual executed results.

Every candidate is validated through :func:`visualization.plan_chart` against
the real rows before it is recommended, so a recommendation can never describe
a chart the executed data does not support. Recommendations never call the
database, never invent columns or values, and never request derived measures
without a verified definition — those belong to the text-to-SQL pipeline.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from time import perf_counter
from typing import Any

from pydantic import Field

from semantic_text2sql.models import (
    ChartRecommendations as ChartRecommendations,
)
from semantic_text2sql.models import (
    ChartSpec,
    ChartType,
    RequestedChange,
    StrictModel,
    TokenUsage,
)
from semantic_text2sql.models import (
    RecommendedChart as RecommendedChart,
)
from semantic_text2sql.runtime import RequestBudget
from semantic_text2sql.visualization import (
    VisualizationError,
    VisualizationIntent,
    _column_roles,
    measure_unit,
    plan_chart,
    requested_plot_columns,
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
    return sum(1 for name in names if re.search(rf"\b{re.escape(name)}\b", question, re.I))


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
            len(distinct) == len(numeric_values) and distinct[-1] - distinct[0] == len(distinct) - 1
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
        "x_components": candidate.x_components,
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


def _chart_matches_request(
    chart: RecommendedChart, requested: set[str], *, strict: bool = False
) -> bool:
    if not requested:
        return True
    used = {chart.y, *chart.y_measures}
    used.update(chart.x_components or [chart.x])
    if chart.series:
        used.add(chart.series)
    # A named plot column must be displayed, not silently replaced by a different one.
    return requested <= used and (not strict or used <= requested)


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
    requested_columns = requested_plot_columns(question, columns)
    strict_columns = bool(re.search(r"\b(?:plot|chart|visuali[sz]e|draw)\b", question, re.I)
                          and len(requested_columns) >= 2
                          and requested_columns.intersection(numeric))
    mentioned = _mentioned(columns, question)
    measures = [
        name
        for name in numeric
        if not _identifier_like(name, [row[columns.index(name)] for row in rows])
    ]
    usable = measures or numeric
    year_column = next(
        (name for name in columns if re.fullmatch(r"(?:calendar_)?year", name, re.I)), None
    )
    month_column = next(
        (name for name in columns if re.fullmatch(r"(?:calendar_)?month", name, re.I)), None
    )
    if year_column and month_column:
        usable = [name for name in usable if name not in {year_column, month_column}]
        if not usable:
            usable = [name for name in numeric if name not in {year_column, month_column}]
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
        x_components: list[str] | None = None,
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
                    x_components=list(x_components or []),
                    y=y,
                    series=series,
                    y_measures=list(y_measures or []),
                    title="",
                    reason=reason,
                    score=0.0,
                ),
            )
        )

    if year_column and month_column:
        primary = primary_measure(mentioned)
        add_candidate(
            "line",
            "Year-Month",
            primary,
            x_components=[year_column, month_column],
            boost=0.45,
            reason="Year and Month form a verified monthly timeline without aggregating rows",
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
            0.35 if wide_family and _mentioned_measures(wide_family, question) >= 2 else 0.1
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
                    reason="grouped bars isolate each "
                    f"{top_category.replace('_', ' ')} within every period",
                )
                if _nonnegative(named_rows, primary) and measure_unit(primary) not in {
                    "ratio",
                    None,
                }:
                    add_candidate(
                        "stacked_area",
                        time_axis,
                        primary,
                        series=top_category,
                        reason=(
                            "stacking additive contributions per "
                            f"{top_category.replace('_', ' ')} keeps the total meaningful"
                        ),
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
            0.35 if wide_family and _mentioned_measures(wide_family, question) >= 2 else 0.1
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
            primary_measure(mentioned),
            boost=boost,
            reason="an observation-distribution chart is the only faithful view",
        )

    # Row-level results can have repeated names/categories but a unique ID.
    # Use that ID only as a low-priority axis; never aggregate repeated labels.
    if len(rows) <= 30 and usable:
        for axis in numeric:
            values = [row[columns.index(axis)] for row in rows]
            if (
                _identifier_like(axis, values)
                and all(value is not None for value in values)
                and len(set(values)) == len(values)
            ):
                add_candidate(
                    "bar", axis, primary_measure(mentioned), boost=-0.3,
                    reason=f"one bar per unique {axis.replace('_', ' ')} preserves each result row",
                )
                break

    ranked: list[RecommendedChart] = []
    seen: set[tuple[Any, ...]] = set()
    for score, candidate in sorted(candidates, key=lambda item: item[0], reverse=True):
        key = (
            candidate.chart_type,
            candidate.x,
            tuple(candidate.x_components),
            candidate.y,
            candidate.series,
            tuple(candidate.y_measures),
        )
        if key in seen or not _chart_matches_request(
            candidate, requested_columns, strict=strict_columns
        ):
            continue
        try:
            spec = plan_chart(
                VisualizationIntent(**_intent_for(candidate)),
                question="",
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


# --- optional LLM candidate-generation stage ---------------------------------

VIZ_RECOMMENDER_LLM_ENABLED_ENV = "TEXT2SQL_VIZ_RECOMMENDER_LLM_ENABLED"
VIZ_RECOMMENDER_LLM_ENABLED = (
    os.environ.get(VIZ_RECOMMENDER_LLM_ENABLED_ENV, "true").casefold() == "true"
)


def viz_recommender_llm_enabled() -> bool:
    return os.environ.get(VIZ_RECOMMENDER_LLM_ENABLED_ENV, "true").casefold() == "true"


class LLMChartProposal(StrictModel):
    """One structured chart proposal from the model; validated before use."""

    chart_type: ChartType
    x: str | None = None
    x_components: list[str] = Field(default_factory=list, max_length=2)
    y: str | None = None
    y_measures: list[str] = Field(default_factory=list, max_length=8)
    series: str | None = None
    reason: str = Field(default="", max_length=300)


class LLMChartProposals(StrictModel):
    """Model output contract: never JavaScript, never ECharts options, never SQL."""

    primary_chart: LLMChartProposal
    alternative_charts: list[LLMChartProposal] = Field(default_factory=list, max_length=3)


_VIZ_PROMPT = """You are an expert Business Analytics Visualization Planner.
Given the current user request, previous validated ChartSpec, typed requested
changes, and executed SQL result, select the most informative and accurate
visualization.

Instructions:
1. Understand whether the user wants a new chart or a modification of the previous chart.
2. For chart-type-only changes, preserve ALL prior chart encodings exactly:
   X-axis, Y-axis, measures, series, and underlying data.
3. For explicitly requested axis or series changes, use only compatible columns
   already present in the executed result.
4. Select charts based on analytical intent: trends, comparisons, distributions,
   relationships, composition, or ranking.
5. Consider column types, cardinality, temporal granularity, metric units, and
   business interpretability.
6. Never silently change the metric, grouping, filters, time period, or underlying analysis.
7. Never invent values, aggregate rows, fill missing data, generate SQL, or produce JavaScript.
8. If different analytical data is required, do not fabricate a chart. The Top-layer
   LLM and governed SQL pipeline own analytical changes.
9. Return only valid JSON matching the existing LLMChartProposals schema: one
   primary chart and up to three meaningful alternatives.

JSON schema shape (return ONLY this object):
{{"primary_chart": {{"chart_type": str, "x": str|null, "x_components": [str], "y": str,
"y_measures": [str], "series": str|null, "reason": str}},
"alternative_charts": [same shape, up to 3]}}
Allowed chart_type values: {chart_types}.
Use only explicitly requested plot columns when the request names two or more.
For separate Year and Month columns, you may set x="Year-Month" and
x_components=["Year", "Month"] for a monthly time axis. Otherwise x_components=[].
The y and y_measures fields must reference numeric result columns. Prefer multiple
measures only when their units are compatible. Keep reason to one short sentence.

Context:
- Current user chart request: {question}
- Previous validated chart: {previous_chart}
- Top-layer requested changes: {chart_changes}
- Preserve existing encodings: {preserve_previous_encodings}
- Required chart type: {required_chart_type}
- Available columns and profiles: {profile}
- Executed row count: {rows_count}
- Bounded sample: {sample}

Prioritize analytical correctness, stakeholder readability, and faithful
preservation of the accepted result.
"""


def _result_profile(
    columns: list[str], rows: list[list[Any]], *, sample_rows: int = 5
) -> tuple[str, str]:
    """Bounded textual profile: role + unit + distinct values per column."""
    numeric, temporal, category = _column_roles(columns, rows)
    named_rows = [dict(zip(columns, row, strict=True)) for row in rows]
    lines: list[str] = []
    for name in columns:
        if name in numeric:
            role = (
                "measure"
                if not _identifier_like(name, [row[name] for row in named_rows])
                else "identifier-like numeric"
            )
        elif name in temporal:
            role = "temporal"
        elif name in category:
            role = "categorical"
        else:
            role = "text"
        unit = measure_unit(name)
        distinct = _cardinality(name, columns, named_rows)
        lines.append(f"{name}: {role}, unit={unit or 'n/a'}, distinct={distinct}")
    sample = [named_rows[index] for index in range(min(sample_rows, len(named_rows)))]
    return "; ".join(lines), str(sample[:sample_rows])


def _validated_rank_chart(
    proposal: LLMChartProposal,
    *,
    columns: list[str],
    rows: list[list[Any]],
    question: str,
    truncated: bool,
    score: float,
    required_chart_type: ChartType | None = None,
    previous_chart: ChartSpec | None = None,
    preserve_previous_encodings: bool = False,
) -> RecommendedChart | None:
    """Validate one LLM proposal through the deterministic chart pipeline."""
    named_columns = set(columns)
    if required_chart_type is not None and proposal.chart_type != required_chart_type:
        return None
    if preserve_previous_encodings and previous_chart is not None:
        proposed_measures = proposal.y_measures or ([proposal.y] if proposal.y else [])
        existing_measures = previous_chart.y_measures or [previous_chart.y]
        if (
            proposal.x != previous_chart.x
            or proposal.x_components != previous_chart.x_components
            or proposal.y != previous_chart.y
            or proposed_measures != existing_measures
            or proposal.series != previous_chart.series
        ):
            return None
    requested_columns = requested_plot_columns(question, columns)
    components = proposal.x_components
    if components and (len(components) != 2 or proposal.x != "Year-Month"):
        return None
    referenced = {
        value
        for value in [
            proposal.y,
            proposal.series,
            *proposal.y_measures,
            *(components or ([proposal.x] if proposal.x is not None else [])),
        ]
        if value is not None
    }
    if not referenced <= named_columns:
        return None
    if proposal.y is None:
        return None
    candidate = RecommendedChart(
        chart_type=proposal.chart_type,
        x=proposal.x or proposal.y,
        x_components=list(components),
        y=proposal.y,
        series=proposal.series,
        y_measures=list(proposal.y_measures),
        title="",
        reason=proposal.reason,
        score=score,
    )
    strict_columns = bool(re.search(r"\b(?:plot|chart|visuali[sz]e|draw)\b", question, re.I)
                          and len(requested_columns) >= 2
                          and requested_columns.intersection(_column_roles(columns, rows)[0]))
    if not _chart_matches_request(candidate, requested_columns, strict=strict_columns):
        return None
    try:
        spec = plan_chart(
            build_recommendation_intent(candidate),
            question="",
            columns=columns,
            rows=rows,
            truncated=truncated,
        )
    except VisualizationError:
        return None
    return candidate.model_copy(update={"title": spec.title, "score": round(score, 3)})


async def refine_charts_with_model(  # noqa: ANN201 - union return
    completer: Any,
    *,
    provider: str,
    model: str,
    question: str,
    columns: list[str],
    rows: list[list[Any]],
    truncated: bool = False,
    timeout_seconds: float = 12.0,
    budget: RequestBudget | None = None,
    required_chart_type: ChartType | None = None,
    previous_chart: ChartSpec | None = None,
    chart_changes: list[RequestedChange] | None = None,
    preserve_previous_encodings: bool = False,
) -> tuple[ChartRecommendations | None, TokenUsage, int]:
    """One bounded model call proposing charts for an executed result.

    Every candidate is re-validated through :func:`plan_chart` before it can
    reach the caller; any parse/timeout/validation failure returns ``None``
    and the deterministic ranking stays authoritative.
    """
    usage = TokenUsage()
    started = perf_counter()
    if truncated or not rows or len(columns) < 2:
        return None, usage, 0
    profile, sample = _result_profile(columns, rows)
    prior_context = (
        json.dumps({
            "chart_type": previous_chart.chart_type,
            "x": previous_chart.x,
            "x_components": previous_chart.x_components,
            "y": previous_chart.y,
            "y_measures": previous_chart.y_measures,
            "series": previous_chart.series,
        }, separators=(",", ":")) if previous_chart is not None else "None"
    )
    requested_changes = json.dumps(
        [change.model_dump(mode="json", exclude_none=True) for change in (chart_changes or [])],
        separators=(",", ":"),
    )
    prompt = _VIZ_PROMPT.format(
        chart_types=", ".join(sorted(_TYPE_BASE)),
        question=question,
        required_chart_type=required_chart_type or "model may choose",
        previous_chart=prior_context,
        chart_changes=requested_changes,
        preserve_previous_encodings=preserve_previous_encodings,
        profile=profile,
        rows_count=len(rows),
        sample=sample,
    )
    try:
        task_completion = getattr(completer, "complete_task", None)
        complete = getattr(completer, "complete_detailed", None)
        if callable(task_completion) or callable(complete):
            if callable(task_completion):
                operation = task_completion(model, prompt, "plotting", json_mode=True)
            else:
                operation = completer.complete_detailed(model=model, prompt=prompt)
            raw, usage = await asyncio.wait_for(
                budget.wait(operation, kind="model") if budget else operation,
                timeout=timeout_seconds,
            )
        else:
            operation = completer.complete(model=model, prompt=prompt)
            raw = await asyncio.wait_for(
                budget.wait(operation, kind="model") if budget else operation,
                timeout=timeout_seconds,
            )
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw).strip(), flags=re.IGNORECASE)
        parsed = LLMChartProposals.model_validate(json.loads(text))
    except Exception:  # noqa: BLE001 - optional chart advice never aborts a completed query
        return None, usage, round((perf_counter() - started) * 1000)

    primary = _validated_rank_chart(
        parsed.primary_chart,
        columns=columns,
        rows=rows,
        question=question,
        truncated=truncated,
        score=1.0,
        required_chart_type=required_chart_type,
        previous_chart=previous_chart,
        preserve_previous_encodings=preserve_previous_encodings,
    )
    if primary is None:
        return None, usage, round((perf_counter() - started) * 1000)
    alternatives: list[RecommendedChart] = []
    seen = {
        (
            primary.chart_type,
            primary.x,
            tuple(primary.x_components),
            primary.y,
            primary.series,
            tuple(primary.y_measures),
        )
    }
    llm_score = 0.95
    for proposal in parsed.alternative_charts:
        validated = _validated_rank_chart(
            proposal,
            columns=columns,
            rows=rows,
            question=question,
            truncated=truncated,
            score=llm_score,
            required_chart_type=required_chart_type,
            previous_chart=previous_chart,
            preserve_previous_encodings=preserve_previous_encodings,
        )
        if validated is None:
            continue
        key = (
            validated.chart_type,
            validated.x,
            tuple(validated.x_components),
            validated.y,
            validated.series,
            tuple(validated.y_measures),
        )
        if key in seen:
            continue
        seen.add(key)
        alternatives.append(validated)
        llm_score -= 0.05
    return (
        ChartRecommendations(primary_chart=primary, alternative_charts=alternatives),
        usage,
        round((perf_counter() - started) * 1000),
    )


def merge_recommendations(
    model_recommended: ChartRecommendations | None,
    deterministic: ChartRecommendations | None,
) -> ChartRecommendations | None:
    """Prefer validated model proposals; backfill from the deterministic ranking."""
    if model_recommended is None:
        return deterministic
    if deterministic is None:
        return model_recommended
    merged_alternatives = [model_recommended.primary_chart, *model_recommended.alternative_charts]
    used = {
        (
            item.chart_type,
            item.x,
            tuple(item.x_components),
            item.y,
            item.series,
            tuple(item.y_measures),
        )
        for item in merged_alternatives
    }
    backfill = [deterministic.primary_chart, *deterministic.alternative_charts]
    for item in backfill:
        key = (
            item.chart_type,
            item.x,
            tuple(item.x_components),
            item.y,
            item.series,
            tuple(item.y_measures),
        )
        if key in used or len(merged_alternatives) >= 4:
            continue
        used.add(key)
        merged_alternatives.append(item)
    return ChartRecommendations(
        primary_chart=merged_alternatives[0],
        alternative_charts=merged_alternatives[1:4],
    )


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
        question="",
        columns=columns,
        rows=rows,
        truncated=truncated,
    )
