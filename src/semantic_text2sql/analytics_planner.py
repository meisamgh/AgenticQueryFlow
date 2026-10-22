"""Typed analytical-intent interpretation for conversational visualization.

The planner converts a conversational message + accepted session state into a
structured :class:`AnalyticsPlan` **proposal**. Deterministic rules run first
and answer most chart follow-ups; an optional, flag-gated model call refines
the plan only when rules cannot confidently interpret the request. Model output
is validated into the same strict contract and never trusted beyond it:

- The plan never bypasses SQL validation; it only describes requested analytics.
- The plan never mutates the accepted semantic contract by itself.
- Unknown dimensions or ambiguous values surface as typed clarifications.

Reusability of stored results is decided by
``semantic_text2sql.data_requirement_validator``, not here.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, cast

from pydantic import Field, ValidationError

from semantic_text2sql.conversation import ConversationState as ConversationState
from semantic_text2sql.models import StrictModel, TokenUsage
from semantic_text2sql.visualization import VisualizationIntent


class RequestedAnalysis(StrictModel):
    metrics: list[str] = Field(default_factory=list, max_length=8)
    dimensions: list[str] = Field(default_factory=list, max_length=6)
    time_grain: Literal["total", "daily", "weekly", "monthly", "quarterly", "yearly"] | None = None
    aggregation: Literal["count", "sum", "average", "min", "max"] | None = None
    derived_measures: list[str] = Field(default_factory=list, max_length=4)


class SemanticChange(StrictModel):
    operation: Literal[
        "CHANGE_GRAIN",
        "CHANGE_METRIC",
        "CHANGE_AGGREGATION",
        "ADD_FILTER",
        "REMOVE_FILTER",
        "REPLACE_FILTER",
        "ADD_DIMENSION",
        "ADD_MEASURE",
        "COMPARE_WITH",
    ]
    target: str = Field(min_length=1, max_length=200)
    old_value: str | int | float | None = None
    new_value: str | int | float | None = None


AcceptedPreservation = dict[str, bool]


class AnalyticsPlan(StrictModel):
    """A proposal of the analytical change behind one conversational turn."""

    operation: Literal[
        "VISUALIZE_EXISTING",
        "CHANGE_GRAIN_AND_VISUALIZE",
        "CHANGE_GROUPING_AND_VISUALIZE",
        "CHANGE_METRIC_AND_VISUALIZE",
        "ADD_FILTER",
        "REMOVE_FILTER",
        "COMPARE",
        "REFINE_AND_VISUALIZE",
        "NEW_ANALYTICAL_QUERY",
        "EXPLAIN_VISUALIZATION",
        "INVESTIGATE_POINT",
        "CLARIFY",
    ]
    depends_on_previous: bool = False
    requested_analysis: RequestedAnalysis = Field(default_factory=RequestedAnalysis)
    semantic_changes: list[SemanticChange] = Field(default_factory=list, max_length=10)
    preserve_existing: AcceptedPreservation = Field(
        default_factory=lambda: {
            "metric_definition": True,
            "filters": True,
            "entities": True,
        }
    )
    visualization_intent: VisualizationIntent | None = None
    clarify: bool = False
    clarification_question: str | None = Field(default=None, max_length=500)
    source: Literal["deterministic", "model", "fallback"] = "deterministic"
    unresolved_terms: list[str] = Field(default_factory=list, max_length=6)


# --- feature flags -----------------------------------------------------------

PLANNER_ENABLED_ENV = "TEXT2SQL_ANALYTICS_PLANNER_ENABLED"
PLANNER_LLM_ENABLED_ENV = "TEXT2SQL_ANALYTICS_PLANNER_LLM_ENABLED"
PLANNER_METRICS_PATH_ENV = "TEXT2SQL_ANALYTICS_METRICS_PATH"


def planner_enabled() -> bool:
    return os.environ.get(PLANNER_ENABLED_ENV, "true").casefold() != "false"


def planner_llm_enabled() -> bool:
    return os.environ.get(PLANNER_LLM_ENABLED_ENV, "false").casefold() == "true"


# --- deterministic interpretation -------------------------------------------

_GRAIN_WORDS = {
    "daily": "daily",
    "weekly": "weekly",
    "monthly": "monthly",
    "quarterly": "quarterly",
    "yearly": "yearly",
    "annual": "yearly",
    "per day": "daily",
    "per week": "weekly",
    "per month": "monthly",
    "per quarter": "quarterly",
    "per year": "yearly",
    "by day": "daily",
    "by week": "weekly",
    "by month": "monthly",
    "by quarter": "quarterly",
    "by year": "yearly",
}
_AGGREGATION_WORDS = {
    "average": "average",
    "mean": "average",
    "total": "sum",
    "sum of": "sum",
    "count of": "count",
    "number of": "count",
    "maximum": "max",
    "minimum": "min",
}
_EXPLAIN_PLOT = re.compile(
    r"\b(?:why|misleading|mislead|explain).{0,30}\b(?:chart|plot|graph|visuali[sz])\b", re.I
)
_COMPARE = re.compile(r"\b(?:compare|versus|vs\.?|also show|next to|alongside)\b", re.I)
_REMOVE = re.compile(r"\b(?:remove|without|exclude|drop|hide)\b", re.I)
# "Plot it per month", "make it a bar chart", "instead show average".
_IMPERATIVE = re.compile(
    r"^(?:and\s+)?(?:now\s+|instead\s+|show\s+|make|turn|use|plot)\b", re.I
)
_DIMENSION = re.compile(
    r"\b(?:per|by|for each|across)\s+([a-z][a-z0-9_ ]{0,40}?)"
    r"(?=\s*(?:\bin\b|\band\b|,|\.|;|!|\?|$))",
    re.I,
)
_ONLY = re.compile(r"\bonly\s+([a-z][a-z0-9_ -]{0,40}?)(?=\s*(?:\band\b|,|\.|;|!|\?|$))", re.I)
_REMOVAL_TARGET = re.compile(
    r"\b(?:remove|without|exclude|drop|hide)\s+(?:all\s+|the\s+)?([a-z][a-z0-9_ -]{0,40}?)"
    r"(?=\s*(?:from|out\b|\band\b|,|\.|;|!|\?|$))",
    re.I,
)
_COMPARE_TARGET = re.compile(
    r"\b(?:compare(?:\s+with|\s+to)?|versus|vs\.?|also\s+show)\s+"
    r"([a-z][a-z0-9_ -]{0,40}?)(?=\s*(?:\band\b|,|\.|;|!|\?|$))",
    re.I,
)
_IN_YEAR = re.compile(r"\b(?:in|during|for)\s+((?:19|20)\d{2})\b", re.I)
_METRIC = re.compile(
    r"\b(?:total|average|avg|mean|sum|count|number of)\s+([a-z][a-z0-9_ -]{0,40}?)"
    r"(?=\s*(?:\bby\b|\bin\b|\bper\b|\bfrom\b|\bto\b|,|\.|;|!|\?|$))",
    re.I,
)
_KPI_WORDS = re.compile(
    r"\b(revenue|cost|profit|margin|consumption|readings|sales|visits|conversions|"
    r"conversion|retention|customers|orders|orders count|users)\b",
    re.I,
)


def _normalize(text: str) -> str:
    return " ".join(text.casefold().split())


def _match_grain(text: str) -> str | None:
    value = _normalize(text)
    for pattern, grain in _GRAIN_WORDS.items():
        if re.search(rf"\b{re.escape(pattern)}\b", value):
            return grain
    return None


def _match_aggregation(text: str) -> str | None:
    value = _normalize(text)
    for pattern, agg in _AGGREGATION_WORDS.items():
        if re.search(rf"\b{re.escape(pattern)}\b", value):
            return agg
    return None


def plan_analytics(
    message: str,
    *,
    viz_intent: VisualizationIntent,
    previous: ConversationState | None,
    db_id: str,
) -> AnalyticsPlan:
    """Deterministically propose the analytical change for this turn.

    Conversational dependency, metrics, dimensions, grain and filter changes
    are extracted with bounded, auditable rules. The previous systate's
    resolved question supplies entity context without leaking other sessions.
    """
    value = _normalize(message)
    unresolved: list[str] = []

    if _EXPLAIN_PLOT.search(value):
        return AnalyticsPlan(
            operation="EXPLAIN_VISUALIZATION",
            depends_on_previous=True,
            visualization_intent=viz_intent,
        )

    grain = _match_grain(message)
    aggregation = _match_aggregation(message)
    dimensions = [
        term.strip() for term in (match.group(1) for match in _DIMENSION.finditer(value)) if term
    ]
    metrics = [match.group(1).strip() for match in _METRIC.finditer(value)]
    kpi_words = _KPI_WORDS.findall(value)
    if not metrics:
        metrics = kpi_words[:4]

    only_values = [match.group(1).strip() for match in _ONLY.finditer(value)]
    removal_values = [match.group(1).strip() for match in _REMOVAL_TARGET.finditer(value)]
    compare_values = [match.group(1).strip() for match in _COMPARE_TARGET.finditer(value)]
    years = _IN_YEAR.findall(value)
    removal = bool(_REMOVE.search(value))
    comparison = bool(_COMPARE.search(value))

    follows = previous is not None and previous.db_id == db_id and bool(
        viz_intent.followup
        or _IMPERATIVE.match(value)
        or re.search(r"\b(?:it|that|this|those|them|again|previous|also)\b", value)
    )
    preserve = {
        "metric_definition": not metrics and not aggregation,
        "filters": not bool(only_values or years or removal_values) and not removal,
        "entities": not comparison,
    }

    changes: list[SemanticChange] = []
    if grain:
        changes.append(SemanticChange(operation="CHANGE_GRAIN", target="time_grain",
                                      new_value=grain))
    if aggregation:
        changes.append(SemanticChange(operation="CHANGE_AGGREGATION", target="aggregation",
                                      new_value=aggregation))
    if metrics:
        changes.append(SemanticChange(operation="CHANGE_METRIC", target="metric",
                                      new_value=", ".join(metrics)))
    for term in dimensions:
        if term:
            changes.append(SemanticChange(operation="ADD_DIMENSION", target=term))
    if removal and removal_values:
        for target in removal_values:
            changes.append(SemanticChange(operation="REMOVE_FILTER", target=target))
    elif removal and only_values:
        for target in only_values:
            changes.append(SemanticChange(operation="REMOVE_FILTER", target=target))
    if not removal and (only_values or years):
        for target in [*only_values, *years]:
            changes.append(SemanticChange(operation="ADD_FILTER", target=target))
    if comparison and previous is not None and previous.last_columns:
        # Comparisons extend the accepted analysis; the conversational pipeline
        # resolves the field through generated SQL, so ambiguity in the stored
        # result never produces a false clarification here.
        for target in compare_values or only_values:
            if target:
                changes.append(SemanticChange(operation="COMPARE_WITH", target=target))

    requested_analysis = RequestedAnalysis(
        metrics=metrics,
        dimensions=dimensions,
        time_grain=cast(Any, grain),
        aggregation=cast(Any, aggregation),
        derived_measures=[],
    )

    return _choose_operation(
        value=value,
        viz_intent=viz_intent,
        follows=follows,
        previous=previous,
        changes=changes,
        preserve=preserve,
        requested=requested_analysis,
        unresolved=unresolved,
    )


def _choose_operation(
    *,
    value: str,
    viz_intent: VisualizationIntent,
    follows: bool,
    previous: ConversationState | None,
    changes: list[SemanticChange],
    preserve: AcceptedPreservation,
    requested: RequestedAnalysis,
    unresolved: list[str],
) -> AnalyticsPlan:
    if viz_intent.unsupported_type:
        raise ValueError(
            f"{viz_intent.unsupported_type.title()} charts are not supported."
        )
    if unresolved:
        return AnalyticsPlan(
            operation="CLARIFY",
            depends_on_previous=follows,
            requested_analysis=requested,
            semantic_changes=changes,
            preserve_existing=preserve,
            visualization_intent=viz_intent,
            clarify=True,
            clarification_question=(
                "Which field should " + " and ".join(unresolved) + " filter by?"
            ),
            unresolved_terms=unresolved,
        )
    chart_requested = viz_intent.requested
    if not chart_requested and not changes:
        return AnalyticsPlan(
            operation="EXPLAIN_VISUALIZATION" if not follows else "REFINE_AND_VISUALIZE",
            depends_on_previous=follows,
            requested_analysis=requested,
            preserve_existing=preserve,
            visualization_intent=viz_intent,
        )
    if chart_requested and follows:
        grain_changed = any(
            change.operation == "CHANGE_GRAIN" for change in changes
        )
        metric_changed = any(
            change.operation in {"CHANGE_METRIC", "CHANGE_AGGREGATION"} for change in changes
        )
        dim_changed = any(change.operation in {"ADD_DIMENSION", "ADD_FILTER"} for change in changes)
        if grain_changed or metric_changed:
            return AnalyticsPlan(
                operation=(
                    "CHANGE_GRAIN_AND_VISUALIZE"
                    if grain_changed
                    else "CHANGE_METRIC_AND_VISUALIZE"
                ),
                depends_on_previous=True,
                requested_analysis=requested,
                semantic_changes=changes,
                preserve_existing=preserve,
                visualization_intent=viz_intent,
                unresolved_terms=unresolved,
            )
        if dim_changed:
            return AnalyticsPlan(
                operation=(
                    "CHANGE_GROUPING_AND_VISUALIZE"
                    if any(change.operation == "ADD_DIMENSION" for change in changes)
                    else "COMPARE"
                    if any(change.operation == "COMPARE_WITH" for change in changes)
                    else "ADD_FILTER"
                ),
                depends_on_previous=True,
                requested_analysis=requested,
                semantic_changes=changes,
                preserve_existing=preserve,
                visualization_intent=viz_intent,
                unresolved_terms=unresolved,
            )
        removed = any(change.operation == "REMOVE_FILTER" for change in changes)
        added_filters = any(change.operation == "ADD_FILTER" for change in changes)
        if removed or added_filters:
            return AnalyticsPlan(
                operation="REMOVE_FILTER" if removed else "ADD_FILTER",
                depends_on_previous=True,
                requested_analysis=requested,
                semantic_changes=changes,
                preserve_existing=preserve,
                visualization_intent=viz_intent,
                unresolved_terms=unresolved,
            )
        return AnalyticsPlan(
            operation="VISUALIZE_EXISTING",
            depends_on_previous=follows,
            requested_analysis=requested,
            preserve_existing=preserve,
            visualization_intent=viz_intent,
            semantic_changes=changes,
        )
    if chart_requested:
        return AnalyticsPlan(
            operation="NEW_ANALYTICAL_QUERY",
            depends_on_previous=False,
            requested_analysis=requested,
            semantic_changes=changes,
            preserve_existing=preserve,
            visualization_intent=viz_intent,
        )
    return AnalyticsPlan(
        operation="ADD_FILTER" if any(c.operation == "ADD_FILTER" for c in changes) else (
            "REMOVE_FILTER" if any(c.operation == "REMOVE_FILTER" for c in changes) else (
                "COMPARE" if any(c.operation == "COMPARE_WITH" for c in changes) else
                "NEW_ANALYTICAL_QUERY"
            )
        ),
        depends_on_previous=follows or any(
            c.operation == "COMPARE_WITH" for c in changes
        ),
        requested_analysis=requested,
        semantic_changes=changes,
        preserve_existing=preserve,
        visualization_intent=viz_intent,
    )


def plan_from_interpretation(
    *,
    interpreted: Any,  # TurnInterpretation
    message: str,
    viz_intent: VisualizationIntent,
) -> AnalyticsPlan:
    """Build a plan from an accepted model ``TurnInterpretation`` when available.

    The interpretation stays authoritative for the operation label; the plan
    adds structure only where the interpretation carries explicit changes.
    """
    changes = [
        SemanticChange(
            operation=cast(Any, change.operation),
            target=change.target or change.operation,
            old_value=change.previous_value,
            new_value=change.new_value,
        )
        for change in interpreted.changes
        if change.operation
        in {
            "ADD_FILTER",
            "REMOVE_FILTER",
            "REPLACE_FILTER",
            "CHANGE_METRIC",
            "CHANGE_GRAIN",
        }
    ]
    requested = RequestedAnalysis(
        dimensions=[change.target for change in changes if change.operation == "ADD_DIMENSION"],
    )
    grain = next(
        (str(change.new_value) for change in changes if change.operation == "CHANGE_GRAIN"), None
    )
    requested.time_grain = cast(Any, grain)
    return AnalyticsPlan(
        operation=cast(Any, interpreted.operation)
        if cast(str, interpreted.operation) in
        {"ADD_FILTER", "REMOVE_FILTER", "CHANGE_METRIC", "CHANGE_GRAIN", "COMPARE", "REFINE"}
        else "NEW_ANALYTICAL_QUERY",
        depends_on_previous=bool(interpreted.depends_on_previous),
        requested_analysis=requested,
        semantic_changes=changes,
        visualization_intent=viz_intent if viz_intent.requested else None,
        source="model",
    )


_PLAN_PROMPT = """Interpret one conversational analytics request as a JSON plan proposal.
Return ONLY one JSON object with keys: operation, depends_on_previous,
requested_analysis{{metrics,dimensions,time_grain,aggregation}}, semantic_changes
(each with operation,target,old_value,new_value), visualization_intent{{requested}}.
Allowed operation values: VISUALIZE_EXISTING, CHANGE_GRAIN_AND_VISUALIZE,
CHANGE_METRIC_AND_VISUALIZE, ADD_FILTER, REMOVE_FILTER, COMPARE, REFINE_AND_VISUALIZE,
NEW_ANALYTICAL_QUERY, EXPLAIN_VISUALIZATION, INVESTIGATE_POINT, CLARIFY.
Allowed time_grain values: total, daily, weekly, monthly, quarterly, yearly, or null.
Allowed aggregation values: count, sum, average, min, max, or null.
Allowed semantic_change operations: CHANGE_GRAIN, CHANGE_METRIC, CHANGE_AGGREGATION,
ADD_FILTER, REMOVE_FILTER, REPLACE_FILTER, ADD_DIMENSION, ADD_MEASURE, COMPARE_WITH.
Rules:
- Never invent columns, values or totals that the user or verified context does not name.
- Preserve constraints the user did not change.
- If the request is ambiguous, set operation to CLARIFY with unresolved_terms as strings.
- Do not generate SQL. Do not generate chart code.
Previous root question: {base}
Previous resolved request: {resolved}
Message: {message}
"""


async def refine_plan_with_model(  # noqa: ANN201 - explicit tuple return below
    completer: Any,
    *,
    provider: str,
    model: str,
    message: str,
    previous: Any,
    plan: AnalyticsPlan,
    timeout_seconds: float = 12.0,
) -> tuple[AnalyticsPlan, TokenUsage, int]:
    """Optionally refine the deterministic plan with one bounded model call.

    Any parse or validation failure returns the deterministic plan untouched
    (``source="fallback"``) so the LLM can never weaken the plan contract.
    Returns the plan, its token usage and the call latency in milliseconds.
    """
    usage = TokenUsage()
    started = perf_counter()
    prompt = _PLAN_PROMPT.format(
        base=getattr(previous, "root_question", "") or "",
        resolved=getattr(previous, "resolved_question", "") or "",
        message=message,
    )
    try:
        complete = getattr(completer, "complete_detailed", None)
        if callable(complete):
            raw, usage = await complete(model=model, prompt=prompt)
        else:
            raw = await completer.complete(model=model, prompt=prompt)
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw).strip(), flags=re.IGNORECASE)
        parsed = AnalyticsPlan.model_validate(json.loads(text))
        refined = parsed.model_copy(update={"source": "model"})
        return refined, usage, round((perf_counter() - started) * 1000)
    except (ValueError, TypeError, AttributeError, ValidationError, json.JSONDecodeError, OSError):
        return plan.model_copy(update={"source": "fallback"}), usage, round(
            (perf_counter() - started) * 1000
        )


def planner_operational(operation: str) -> bool:
    """Whether a plan operation needs interpretation beyond chart mechanics."""
    return operation not in {"VISUALIZE_EXISTING", "EXPLAIN_VISUALIZATION"}


# --- planner metrics ledger ---------------------------------------------------


class PlannerDecisionRecord(StrictModel):
    """One recorded planner turn; decisions only, never row data.

    ``source`` distinguishes the deterministic rules ("deterministic"), a
    validated model refinement ("model"), a model failure that fell back to
    the deterministic proposal ("fallback"), and turns where the planner was
    disabled ("disabled").
    """

    recorded_at: str = Field(max_length=40)
    session_id: str = Field(default="", max_length=100)
    db_id: str = Field(default="", max_length=100)
    source: Literal["deterministic", "model", "fallback", "disabled"] = "deterministic"
    operation: str | None = Field(default=None, max_length=100)
    reusability_decision: str | None = Field(default=None, max_length=100)
    requery: bool = False
    latency_ms: int = Field(default=0, ge=0)
    planner_llm_enabled: bool = False


class PlannerMetricsLedger:
    """Append-only in-process ledger of planner decisions.

    When a metrics path is configured each record is also appended as one JSONL
    line; write failures never affect the request path.
    """

    def __init__(self, path: Path | None = None, *, max_entries: int = 1_000) -> None:
        self._path = path
        self._max_entries = max_entries
        self._entries: list[PlannerDecisionRecord] = []

    def record(self, entry: PlannerDecisionRecord) -> None:
        self._entries.append(entry)
        if len(self._entries) > self._max_entries:
            del self._entries[: len(self._entries) - self._max_entries]
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(entry.model_dump_json() + "\n")
        except OSError:  # pragma: no cover - metrics must never break a request
            pass

    def summarize(self) -> dict[str, int]:
        counts: dict[str, int] = {
            "turns": len(self._entries),
            "deterministic": 0,
            "model": 0,
            "fallback": 0,
            "disabled": 0,
            "requery": 0,
            "reuse": 0,
        }
        for entry in self._entries:
            counts[entry.source] = counts.get(entry.source, 0) + 1
            if entry.requery:
                counts["requery"] += 1
            elif entry.reusability_decision == "reuse_result":
                counts["reuse"] += 1
        return counts

    def entries(self) -> list[PlannerDecisionRecord]:
        return list(self._entries)


_ledger: PlannerMetricsLedger | None = None


def metrics_ledger() -> PlannerMetricsLedger:
    """Process-wide ledger; the JSONL path is read from the environment once."""
    global _ledger
    if _ledger is None:
        configured = os.environ.get(PLANNER_METRICS_PATH_ENV, "").strip()
        _ledger = PlannerMetricsLedger(Path(configured) if configured else None)
    return _ledger


def reset_metrics_ledger() -> None:
    global _ledger
    _ledger = None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")
