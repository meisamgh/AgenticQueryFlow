"""Explicit-request chart planning over complete, already executed result rows.

Rules select a chart; SQL, row values, and analytical grain stay authoritative.
No plotting code or chart-library options are accepted from a model or the client.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from statistics import median
from typing import Any, Literal, cast

from semantic_text2sql.models import ChartSpec, ChartType, StrictModel

# Longest and most specific expressions first. A generic "show" is never a plot request.
CHART_PATTERNS: tuple[tuple[ChartType, str], ...] = (
    ("calendar_heatmap", r"\bcalendar\s+heat\s*map\b"),
    ("stacked_area", r"\bstacked\s+area\b"),
    ("stacked_bar", r"\bstacked\s+(?:bar|column)\b"),
    ("grouped_bar", r"\b(?:grouped|clustered)\s+(?:bar|column)\b"),
    ("horizontal_bar", r"\bhorizontal\s+(?:bar|column)\b"),
    ("multi_line", r"\bmulti[- ]?line\b"),
    ("step_line", r"\bstep(?:ped)?[- ]?line\b"),
    ("box", r"\bbox\s*(?:and\s*whisker\s*)?plot\b"),
    ("heatmap", r"\bheat\s*map\b"),
    ("treemap", r"\btree\s*map\b"),
    ("histogram", r"\bhistogram\b"),
    ("bubble", r"\bbubble\s*(?:plot|chart)?\b"),
    ("scatter", r"\bscatter\s*(?:plot|chart)?\b"),
    ("donut", r"\b(?:donut|doughnut)\s*(?:chart|plot)?\b"),
    ("pie", r"\bpie\s*(?:chart|plot)?\b"),
    ("area", r"\barea\s*(?:chart|plot)?\b"),
    ("line", r"\bline\s*(?:chart|plot|graph)\b"),
    ("bar", r"\b(?:bar|column)\s*(?:chart|plot|graph)\b"),
)
_REQUEST = re.compile(
    r"\b(?:plot|chart|graph|visuali[sz]e|draw|diagram|histogram|heatmap|"
    r"treemap|box\s*plot|scatter\s*plot|bubble\s*plot|donut\s*chart|pie\s*chart)\b",
    re.I,
)
_FOLLOWUP = re.compile(r"\b(?:it|that|this|previous|last|instead|horizontal|vertical)\b", re.I)
_HORIZONTAL = re.compile(r"\bhorizontal\b", re.I)
_VERTICAL = re.compile(r"\bvertical\b", re.I)
_PER = re.compile(
    r"\b(?:grouped\s+by|per)\s+((?:[A-Za-z_][A-Za-z0-9_]*)(?:\s+[A-Za-z_][A-Za-z0-9_]*){0,2}?)"
    r"(?=\s+(?:in\s+(?:19|20)\d\d\b|as\b)|[,.;!?]|\s*$)",
    re.I,
)
_BY = re.compile(
    r"\bby\s+((?:[A-Za-z_][A-Za-z0-9_]*)(?:\s+[A-Za-z_][A-Za-z0-9_]*){0,2}?)"
    r"(?:\s+and\s+(([A-Za-z_][A-Za-z0-9_]*)(?:\s+[A-Za-z_][A-Za-z0-9_]*){0,2}?))?"
    r"(?=\s+(?:as|and|in\s+(?:19|20)\d\d|of|for|per|with|over|during|between)\b|[,.;!?]|\s*$)",
    re.I,
)
_VERSUS = re.compile(
    r"\b(?:against|versus|vs\.?)\s+([A-Za-z_][A-Za-z0-9_]*(?:\s+[A-Za-z_][A-Za-z0-9_]*)*?)"
    r"(?=\s+(?:in\s+(?:19|20)\d\d|by|for|per|over)\b|[,.;!?]|\s*$)",
    re.I,
)
_SIZED = re.compile(
    r"\bsi[sz]ed?\s+by\s+([A-Za-z_][A-Za-z0-9_]*(?:\s+[A-Za-z_][A-Za-z0-9_]*)*?)"
    r"(?=\s+(?:as|and|in|by|per)\b|[,.;!?]|\s*$)",
    re.I,
)
_GRAIN = re.compile(r"\b(daily|monthly|yearly|weekly)\b", re.I)
_DATE_SHAPE = re.compile(r"(?:\d{4}|\d{6}|\d{4}-\d{2}|\d{4}-\d{2}-\d{2})\Z")


class VisualizationIntent(StrictModel):
    requested: bool
    chart_type: ChartType | None = None
    explicit_type: bool = False
    unsupported_type: str | None = None
    followup: bool = False
    x: str | None = None
    y: str | None = None
    series: str | None = None
    size: str | None = None
    aggregation: str | None = None
    time_grain: str | None = None


class VisualizationError(ValueError):
    """A plot cannot be drawn faithfully from this result."""


def parse_intent(question: str, *, has_previous: bool = False) -> VisualizationIntent:
    match: tuple[ChartType | None, str] | None = next(
        ((kind, pattern) for kind, pattern in CHART_PATTERNS if re.search(pattern, question, re.I)),
        None,
    )
    requested = bool(match or _REQUEST.search(question))
    orientation_word = _HORIZONTAL.search(question) or _VERTICAL.search(question)
    if not requested:
        if orientation_word and has_previous and _FOLLOWUP.search(question):
            # "Make it horizontal" only refers to an existing chart.
            requested = True
        else:
            return VisualizationIntent(requested=False)
    unsupported = re.search(
        r"\b(radar|sankey|gauge|waterfall|funnel|candlestick)\s+(?:chart|plot|graph)\b",
        question,
        re.I,
    )
    # Only a genuinely referential request can reuse an earlier result.
    followup = has_previous and bool(
        _FOLLOWUP.search(question)
        or re.match(r"^\s*(?:make|turn|use)\b", question, re.I)
        and not re.search(r"\b(?:by|of|for|in 20\d\d)\b", question, re.I)
    )
    # Deterministic structural extraction: chart family first, then axes,
    # series and bubble-size dims when the request names them unambiguously.
    if match is None:
        if _HORIZONTAL.search(question) and followup:
            match = ("horizontal_bar", "")
        elif _VERTICAL.search(question) and followup:
            match = ("bar", "")
        elif _GRAIN.search(question):
            match = (None, "")
    per = _PER.search(question)
    per_name = per.group(1).strip().casefold() if per else None
    by_dims: list[str] = []
    for first, second, _third in _BY.findall(question):
        by_dims.append(first.strip().casefold())
        if second:
            by_dims.append(second.strip().casefold())
    versus = _VERSUS.search(question)
    sized = _SIZED.search(question)
    multi_family = bool(
        match
        and match[0]
        in {"multi_line", "stacked_area", "grouped_bar", "stacked_bar"}
    )
    if multi_family:
        # A grouped/stacked/multi-series request maps the named dimension(s) to
        # the series; the grain word ("monthly") keeps the x axis temporal.
        series = per_name or (by_dims[0] if by_dims else None)
        x = by_dims[1] if len(by_dims) > 1 else None
    else:
        # "per X" / "by X" names the x-axis dimension for single-series charts
        # ("races per year", "revenue by month"); a second "by" is the series.
        series = by_dims[1] if len(by_dims) > 1 else None
        x = by_dims[0] if by_dims else per_name
    return VisualizationIntent(
        requested=True,
        chart_type=match[0] if match else None,
        explicit_type=match is not None and bool(match[1] or match[0]),
        followup=followup,
        x=x,
        y=versus.group(1).strip().casefold() if versus else None,
        series=series,
        size=sized.group(1).strip().casefold() if sized else None,
        unsupported_type=unsupported.group(1).lower() if unsupported else None,
    )


def sql_output_requirement(intent: VisualizationIntent) -> str | None:
    """A bounded, structural SQL-output hint; never a plotting instruction."""
    if not intent.requested:
        return None
    kind = intent.chart_type
    if kind in {"stacked_bar", "stacked_area", "grouped_bar", "multi_line"}:
        shape = "one x-axis dimension, one series dimension, and one numeric measure"
    elif kind in {"scatter"}:
        shape = "two numeric measures at the same observation grain"
    elif kind == "bubble":
        shape = "two numeric measures plus a separate numeric size measure"
    elif kind in {"heatmap", "treemap"}:
        shape = "two categorical dimensions and one numeric measure"
    elif kind == "calendar_heatmap":
        shape = "daily dates and one numeric measure"
    elif kind in {"histogram", "box"}:
        shape = "individual numeric observations, or explicit verified distribution summaries"
    else:
        shape = "a distinct dimension and a numeric measure at the requested grain"
    return (
        f"Visualization data requirement: return {shape} as separate named SQL columns. "
        "Respect the user's metrics, filters and grain. Do not generate plotting code."
    )


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _date_value(value: Any) -> date | None:
    text = str(value)
    if not _DATE_SHAPE.fullmatch(text):
        return None
    try:
        if len(text) == 4:
            return date(int(text), 1, 1)
        if len(text) == 6:
            return datetime.strptime(text, "%Y%m").date()
        if len(text) == 7:
            return datetime.strptime(text, "%Y-%m").date()
        return date.fromisoformat(text)
    except ValueError:
        return None


def _column_roles(
    columns: list[str], rows: list[list[Any]]
) -> tuple[list[str], list[str], list[str]]:
    numeric: list[str] = []
    temporal: list[str] = []
    category: list[str] = []
    for index, name in enumerate(columns):
        values = [row[index] for row in rows]
        if all(_number(value) for value in values):
            numeric.append(name)
        elif all(_date_value(value) is not None for value in values):
            temporal.append(name)
        else:
            category.append(name)
    return numeric, temporal, category


def _resolve_dimension(raw: Any, columns: list[str]) -> str | None:
    """Map a free-text dimension mention onto an actual result column."""

    if not raw:
        return None
    if isinstance(raw, str) and raw in columns:
        return raw
    text = re.sub(r"[_\s-]+", " ", str(raw).strip().casefold())
    if not text:
        return None

    def norm(name: str) -> str:
        return re.sub(r"[_\s-]+", " ", name.casefold()).strip()

    exact = next((name for name in columns if norm(name) == text), None)
    if exact:
        return exact
    first_word = text.split()[0]
    return next(
        (
            name
            for name in columns
            if text in norm(name) or norm(name) == first_word
        ),
        None,
    )


def _mentioned(columns: list[str], text: str) -> str | None:
    normalized = text.casefold().replace("_", " ")
    matches = [
        name
        for name in columns
        if re.search(rf"(?<!\w){re.escape(name.casefold().replace('_', ' '))}(?!\w)", normalized)
    ]
    return max(matches, key=len) if matches else None


def _quantile(values: list[float], q: float) -> float:
    offset = (len(values) - 1) * q
    lo = int(math.floor(offset))
    hi = int(math.ceil(offset))
    return values[lo] + (values[hi] - values[lo]) * (offset - lo)


def plan_chart(
    intent: VisualizationIntent,
    *,
    question: str,
    columns: list[str],
    rows: list[list[Any]],
    truncated: bool,
) -> ChartSpec:
    if not intent.requested:
        raise VisualizationError("No visualization was requested.")
    if intent.unsupported_type:
        raise VisualizationError(f"{intent.unsupported_type.title()} charts are not supported.")
    if truncated:
        raise VisualizationError(
            "The result was truncated. Increase the read-only result limit or "
            "narrow the question before plotting."
        )
    if not rows:
        raise VisualizationError("The query returned no rows to plot.")
    if len(columns) != len(set(columns)) or any(len(row) != len(columns) for row in rows):
        raise VisualizationError("The result has ambiguous or incomplete columns.")
    if len(rows) > 500:
        raise VisualizationError("The result is too large to plot without a verified reduction.")
    if any(value is None for row in rows for value in row):
        raise VisualizationError(
            "The result contains NULL values; clarify how missing points should be shown."
        )
    if any(isinstance(value, float) and not math.isfinite(value) for row in rows for value in row):
        raise VisualizationError("The result contains a non-finite number.")
    numeric, temporal, category = _column_roles(columns, rows)
    requested = intent.chart_type
    if requested is None:
        if re.search(r"\b(distribution|frequency)\b", question, re.I) and len(numeric) == 1:
            requested = "histogram"
        elif (
            re.search(r"\b(correlation|against|relationship between)\b", question, re.I)
            and len(numeric) >= 2
        ):
            requested = "scatter"
        elif temporal and numeric:
            requested = "multi_line" if len(category) == 1 else "line"
        elif category and numeric:
            requested = "grouped_bar" if len(category) == 2 else "bar"
        else:
            raise VisualizationError("Specify a chart type and the columns to plot.")
    x = _resolve_dimension(intent.x, columns) if intent.x else intent.x
    y = _resolve_dimension(intent.y, columns) if intent.y else intent.y
    series = _resolve_dimension(intent.series, columns) if intent.series else intent.series
    size = _resolve_dimension(intent.size, columns) if intent.size else intent.size
    use_series = requested in {"multi_line", "stacked_area", "grouped_bar", "stacked_bar"}
    point_series = requested in {"scatter", "bubble"}
    frequency = next(
        (
            name
            for name in numeric
            if re.search(r"(?:^|_)(?:count|frequency|freq)(?:$|_)", name, re.I)
            and all(
                float(row[columns.index(name)]).is_integer() and row[columns.index(name)] >= 0
                for row in rows
            )
        ),
        None,
    )
    bin_column = next((name for name in columns if name != frequency), None)
    pre_binned = requested == "histogram" and frequency is not None and len(columns) == 2
    five_names = {
        key: next((name for name in numeric if name.casefold() in alternatives), None)
        for key, alternatives in {
            "min": {"min", "minimum"}, "q1": {"q1", "first_quartile"},
            "median": {"median"}, "q3": {"q3", "third_quartile"},
            "max": {"max", "maximum"},
        }.items()
    }
    pre_summarized = requested == "box" and all(five_names.values())
    if requested in {"histogram", "box"}:
        y = y or (
            five_names["median"]
            if pre_summarized
            else frequency
            if pre_binned
            else numeric[0]
            if len(numeric) == 1
            else _mentioned(numeric, question)
        )
        x = x or (bin_column if pre_binned else category[0] if category else y)
    elif requested == "calendar_heatmap":
        x = x or (temporal[0] if len(temporal) == 1 else None)
        y = y or (numeric[0] if len(numeric) == 1 else None)
    elif point_series:
        x = x or (numeric[0] if numeric else None)
        y = y or (next((name for name in numeric if name != x), None))
        if requested == "bubble":
            size = size or next((name for name in numeric if name not in {x, y}), None)
    elif requested in {"heatmap", "treemap"}:
        x = x or (category[0] if category else None)
        series = series or next((name for name in category if name != x), None)
        y = y or (numeric[0] if len(numeric) == 1 else _mentioned(numeric, question))
    else:
        x = x or (temporal[0] if temporal else category[0] if category else None)
        remaining_numeric = [name for name in numeric if name != x]
        mentioned = _mentioned(numeric, question)
        if mentioned == x:
            mentioned = None
        y = y or mentioned or (
            remaining_numeric[0]
            if x and len(remaining_numeric) == 1
            else numeric[0]
            if len(numeric) == 1
            else None
        )
        if use_series:
            series = series or next((name for name in category if name != x), None)
    if (
        not x
        or not y
        or x not in columns
        or y not in columns
        or (x == y and requested not in {"histogram", "box"})
    ):
        raise VisualizationError("The result does not contain unambiguous chart axes.")
    if y not in numeric:
        raise VisualizationError(f"{y} must contain finite numerical values.")
    if requested in {
        "line", "multi_line", "step_line", "area", "stacked_area", "calendar_heatmap"
    } and (
        x not in temporal and x not in numeric
    ):
        raise VisualizationError("This chart needs an ordered date or numeric axis.")
    if requested == "calendar_heatmap" and (
        x not in temporal or any(len(str(row[columns.index(x)])) != 10 for row in rows)
    ):
        raise VisualizationError("A calendar heatmap requires daily YYYY-MM-DD dates.")
    if point_series and x not in numeric:
        raise VisualizationError("Scatter and bubble charts need a numeric x-axis.")
    if requested == "bubble" and (size not in numeric or size in {x, y}):
        raise VisualizationError("A bubble chart needs a separate numeric size column.")
    if use_series and (series not in columns or series == x or series in numeric):
        raise VisualizationError("This chart needs a separate category or series column.")
    if requested in {"heatmap", "treemap"} and (series not in columns or series == x):
        raise VisualizationError("This chart needs two distinct category columns.")
    if requested in {"pie", "donut", "treemap", "stacked_bar", "stacked_area"} and any(
        row[columns.index(y)] < 0 for row in rows
    ):
        raise VisualizationError("This composition chart requires nonnegative measures.")
    if requested in {"pie", "donut", "treemap"} and not any(
        row[columns.index(y)] > 0 for row in rows
    ):
        raise VisualizationError("A composition chart needs a positive total.")
    if requested in {"pie", "donut"} and x not in category:
        raise VisualizationError("A pie or donut chart needs a categorical whole.")
    if requested == "box" and len(rows) < 5 and not pre_summarized:
        raise VisualizationError("A box plot needs at least five observations.")
    if requested == "histogram" and len(rows) < 2:
        raise VisualizationError("A histogram needs at least two observations.")
    warnings: list[str] = []
    if requested not in {"scatter", "bubble", "histogram", "box"}:
        keys = [
            (row[columns.index(x)], row[columns.index(series)] if series else None) for row in rows
        ]
        if len(keys) != len(set(keys)):
            raise VisualizationError(
                "Multiple rows share a chart position; define the aggregation in SQL first."
            )
        if len(set(keys)) > 200:
            raise VisualizationError("Too many chart positions; narrow the result before plotting.")
        if series:
            x_values = {key[0] for key in keys}
            series_values = {key[1] for key in keys}
            if len(keys) < len(x_values) * len(series_values):
                if requested in {"stacked_bar", "stacked_area"}:
                    raise VisualizationError(
                        "A stacked chart needs every series at every x value; "
                        "missing data cannot be zero-filled."
                    )
                warnings.append(
                    "Some series are missing at some x values; gaps are shown, not zeros."
                )
    if requested in {"line", "multi_line", "step_line", "area", "stacked_area"} and len(rows) < 2:
        raise VisualizationError("A trend chart needs at least two time points.")

    raw: list[dict[str, str | int | float | None]] = [
        {
            name: row[index] if isinstance(row[index], (str, int, float)) else str(row[index])
            for index, name in enumerate(columns)
        }
        for row in rows
    ]
    transformation: Literal["none", "histogram", "five_number_summary"] = "none"
    if requested == "histogram" and not pre_binned:
        values = [float(row[columns.index(y)]) for row in rows]
        lo, hi = min(values), max(values)
        bins = max(1, min(20, math.ceil(math.sqrt(len(values))))) if lo < hi else 1
        width = (hi - lo) / bins if bins > 1 else 1.0
        counts = [0] * bins
        for value in values:
            counts[min(bins - 1, int((value - lo) / width))] += 1
        raw = [
            {"bin_start": lo + idx * width, "bin_end": lo + (idx + 1) * width, "count": count}
            for idx, count in enumerate(counts)
        ]
        transformation, x, y = "histogram", "bin_start", "count"
    elif requested == "histogram":
        if len({str(row[columns.index(x)]) for row in rows}) != len(rows):
            raise VisualizationError("Histogram bins must be unique.")
    elif requested == "box" and pre_summarized:
        raw = []
        for index, row in enumerate(rows):
            numbers = [float(row[columns.index(cast(str, five_names[key]))])
                       for key in ("min", "q1", "median", "q3", "max")]
            if numbers != sorted(numbers):
                raise VisualizationError(
                    "Five-number summaries must be ordered min, Q1, median, Q3, max."
                )
            group = str(row[columns.index(x)]) if x != y else f"Summary {index + 1}"
            raw.append(
                dict(zip(("min", "q1", "median", "q3", "max"), numbers, strict=True))
                | {"group": group}
            )
        if len({item["group"] for item in raw}) != len(raw):
            raise VisualizationError("Five-number summary group labels must be unique.")
        x, y = "group", "median"
    elif requested == "box":
        groups = (
            list(dict.fromkeys(row[columns.index(x)] for row in rows))
            if x != y
            else ["All observations"]
        )
        raw = []
        for group in groups:
            values = sorted(
                float(row[columns.index(y)])
                for row in rows
                if x == y or row[columns.index(x)] == group
            )
            if len(values) < 5:
                raise VisualizationError("Each box-plot group needs at least five observations.")
            raw.append(
                {
                    "group": str(group),
                    "min": values[0],
                    "q1": _quantile(values, 0.25),
                    "median": median(values),
                    "q3": _quantile(values, 0.75),
                    "max": values[-1],
                }
            )
        transformation, x, y = "five_number_summary", "group", "median"
    if requested in {"line", "multi_line", "step_line", "area", "stacked_area", "calendar_heatmap"}:
        raw.sort(
            key=lambda item: (
                (_date_value(item[x]) or date.min) if x in temporal else float(str(item[x]))
            )
        )
    title = f"{y.replace('_', ' ')} by {x.replace('_', ' ')}"
    magnitude_column = intent.y or y
    magnitudes = (
        [abs(float(row[columns.index(magnitude_column)])) for row in rows]
        if magnitude_column in columns
        else [0.0]
    )
    time_format: Literal["raw", "year", "month", "day"] = "raw"
    if x in temporal:
        sample = str(rows[0][columns.index(x)])
        time_format = {4: "year", 6: "month", 7: "month", 10: "day"}.get(len(sample), "raw")  # type: ignore[assignment]
    return ChartSpec(
        chart_type=requested,
        source_columns=columns,
        source_row_count=len(rows),
        x=x,
        y=y,
        series=series,
        size=size,
        title=title,
        x_label=x.replace("_", " "),
        y_label=y.replace("_", " "),
        legend=bool(series),
        transformation=transformation,
        data=raw,
        warnings=warnings,
        number_format="compact" if max(magnitudes) >= 10_000 else "plain",
        time_format=time_format,
    )
