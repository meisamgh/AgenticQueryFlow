"""Deterministic result-reusability gate for conversational analytics.

The gate decides whether an already-executed, complete result can answer a new
analytical request, or whether validated new SQL is required. Metric semantics
and result grain decide safety — never column names alone. The gate never infers
detailed data from an aggregate and never treats a numeric column as a proven
grouping dimension.

Decisions:
- ``reuse_result``   : the complete stored rows already contain the requested
  analysis at the requested grain; charting is presentation-only.
- ``requery_database``: new data is needed; the existing text-to-SQL pipeline
  must generate and execute validated SQL.
- ``need_clarification``: the stored result exists but cannot support the
  request faithfully (truncated, empty), or the request is ambiguous.
- ``unsupported``    : the request cannot be satisfied safely even with new SQL
  produced by this pipeline.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from semantic_text2sql.models import StrictModel

if TYPE_CHECKING:  # pragma: no cover - type-only import cycle guard
    pass

Decision = Literal["reuse_result", "requery_database", "need_clarification", "unsupported"]

_GRAINS: dict[str, int] = {
    "total": 0,
    "yearly": 4,
    "quarterly": 8,
    "monthly": 12,
    "weekly": 20,
    "daily": 40,
}

_NON_ADDITIVE = re.compile(
    r"(?:\b|_)(?:average|mean|rate|ratio|margin|percentage|percent|share|"
    r"pct|conversion|retention|distinct|median|quantile|avg)(?:\b|_)",
    re.I,
)


class DataRequirement(StrictModel):
    decision: Decision
    reason: str
    requested_dimensions: list[str] = Field(default_factory=list)
    requested_grain: str | None = None
    requested_metrics: list[str] = Field(default_factory=list)
    additivity_note: str | None = None
    reuse_dimensions: list[str] = Field(default_factory=list)


def _stored_grain_capability(stored_columns: list[str]) -> int:
    """Finest guaranteed grain implied by the stored columns (0 = aggregate)."""
    best = 0
    name = " ".join(column.replace("_", " ") for column in stored_columns).casefold()
    if re.search(r"\b(?:day|daily|date)\b", name):
        best = _GRAINS["daily"]
    elif re.search(r"\b(?:week|weekly)\b", name):
        best = _GRAINS["weekly"]
    elif re.search(r"\b(?:month|monthly)\b", name):
        best = _GRAINS["monthly"]
    elif re.search(r"\b(?:quarter|quarterly)\b", name):
        best = _GRAINS["quarterly"]
    elif re.search(r"\b(?:year|yearly|annual)\b", name):
        best = _GRAINS["yearly"]
    return best


def _non_additive_metric(name: str) -> bool:
    return bool(_NON_ADDITIVE.search(name))


def validate_reusability(
    plan: Any,  # AnalyticsPlan; kept untyped to avoid the import cycle
    *,
    stored_columns: list[str] | None,
    stored_rows: list[list[object]] | None,
    stored_row_count: int | None,
    stored_truncated: bool,
    approved_metrics: dict[str, Any] | None = None,
) -> DataRequirement:
    """Decide how the accepted previous result relates to the requested analysis.

    ``None`` stored result data means nothing executable is reliably available:
    stored state without row snapshots cannot answer any visualization request.
    ``approved_metrics`` are typed entries from the verified semantic catalog;
    when supplied, the gate reports derived-metric lineage in its reasons.
    """
    requested = plan.requested_analysis
    dimensions = requested.dimensions
    grain = requested.time_grain
    metrics = requested.metrics
    wants_chart = plan.visualization_intent.requested if plan.visualization_intent else False

    def outcome(
        decision: Decision,
        reason: str,
        *,
        additivity_note: str | None = None,
        reuse_dimensions: list[str] | None = None,
    ) -> DataRequirement:
        return DataRequirement(
            decision=decision,
            reason=reason,
            requested_dimensions=dimensions,
            requested_grain=grain,
            requested_metrics=metrics,
            additivity_note=additivity_note,
            reuse_dimensions=reuse_dimensions or [],
        )

    if plan.clarify or plan.unresolved_terms:
        return outcome(
            "need_clarification",
            plan.clarification_question or "Please clarify the requested analytical change.",
        )

    if not wants_chart and not dimensions and not grain and not metrics:
        return outcome("unsupported", "No analytical request could be extracted.")

    if stored_columns is None or stored_row_count is None:
        return outcome(
            "need_clarification",
            "The complete previous result is unavailable. Ask the original question again "
            "with the plot request.",
        )
    if stored_row_count == 0 or not stored_rows:
        return outcome(
            "need_clarification",
            "The previous query returned no rows that could be visualized.",
        )
    if stored_truncated:
        return outcome(
            "need_clarification",
            "The saved result was truncated, so the complete data cannot be reused safely.",
        )
    if stored_row_count != len(stored_rows):
        return outcome(
            "need_clarification",
            "The saved result is incomplete; ask the original question again with this change.",
        )

    non_additive = [name for name in metrics if _non_additive_metric(name)]
    additivity_note = (
        "Requested metrics are non-additive: "
        + ", ".join(sorted(non_additive))
        + ". They require recomputation through validated SQL."
        if non_additive
        else None
    )

    # Governed derived measures (profit margin, revenue per customer, ...) are
    # always recomputed through validated SQL: the verified definition belongs
    # to the catalog, and deriving ratios from stored client-side aggregates
    # would risk averages-of-averages and silent unit mismatches.
    if plan.requested_analysis.derived_measures:
        derived = plan.requested_analysis.derived_measures
        return outcome(
            "requery_database",
            "Derived metric(s) "
            + ", ".join(derived)
            + " are governed by an approved definition and must be recomputed "
            "through validated SQL to preserve lineage and aggregation semantics.",
            additivity_note=(
                "Derived measures are treated as non-additive: their definition "
                "recomputes for every requested grain."
            ),
        )

    # A stored aggregate does not prove a new filter, metric or comparison.
    # Treat these as SQL changes unless an explicit display-only action has
    # been checked against complete result rows elsewhere.
    sql_changes = {
        "ADD_FILTER",
        "REMOVE_FILTER",
        "REPLACE_FILTER",
        "CHANGE_METRIC",
        "CHANGE_AGGREGATION",
        "ADD_MEASURE",
        "COMPARE_WITH",
    }
    changed = [item.operation for item in plan.semantic_changes if item.operation in sql_changes]
    if changed:
        return outcome(
            "requery_database",
            "The request changes the underlying analysis ("
            + ", ".join(changed)
            + "); generate and validate new SQL.",
            additivity_note=additivity_note,
        )
    available_metrics = {col.casefold() for col in stored_columns}
    missing_metrics = [name for name in metrics if name.casefold() not in available_metrics]
    if missing_metrics:
        return outcome(
            "requery_database",
            "The stored result does not contain the requested metric(s): "
            + ", ".join(missing_metrics)
            + ".",
            additivity_note=additivity_note,
        )

    # Any requested grouping that is absent from the stored rows forces new SQL.
    # A finance "stored as month | country" example: "now by region" has no
    # region column, so no amount of client-side work can fabricate it.
    missing_dimensions = [name for name in dimensions if name not in stored_columns]
    if missing_dimensions:
        return outcome(
            "requery_database",
            "The stored result lacks the requested dimension(s): "
            + ", ".join(missing_dimensions)
            + ". Preserving the earlier metric and filters, new validated SQL is required.",
            additivity_note=additivity_note,
        )

    if grain:
        stored_capability = _stored_grain_capability(stored_columns)
        requested_capability = _GRAINS.get(grain, 0)
        if requested_capability == 0:
            # "Show the total": a re-aggregation of any stored grain.
            if non_additive or stored_row_count == 1:
                return outcome(
                    "reuse_result" if stored_row_count == 1 else "requery_database",
                    "Totals of non-additive metrics must be recomputed through SQL."
                    if non_additive
                    else "The stored result is already the requested aggregate.",
                    additivity_note=additivity_note,
                )
            return outcome(
                "reuse_result",
                "The complete stored rows support this request without new SQL.",
            )
        if requested_capability > stored_capability:
            return outcome(
                "requery_database",
                "The stored result aggregates at a coarser grain ("
                f"{stored_capability or 'total'}) than requested ({grain}). "
                "Detail cannot be inferred from an aggregate; new SQL preserves the "
                "earlier metric and filters.",
                additivity_note=additivity_note,
            )
        if requested_capability < stored_capability:
            # Coarser regroup (monthly -> yearly): additive sums are safe client
            # side, but the plan restricts reuse to match SQL semantics exactly.
            if non_additive:
                return outcome(
                    "requery_database",
                    "The stored rows are at a finer grain than requested for a "
                    "non-additive metric; the aggregation must happen in SQL.",
                    additivity_note=additivity_note
                    or ("Non-additive metrics cannot be reaggregated from stored rows."),
                )
            return outcome(
                "requery_database",
                "The stored result is at a finer grain than requested; regrouping is "
                "an analytical change handled by validated SQL for unambiguous grain.",
                additivity_note=additivity_note,
            )

    # Same-or-coarser requested grain with all dimensions present: reuse.
    reuse_dimensions = [name for name in dimensions if name in stored_columns]
    return outcome(
        "reuse_result",
        "The complete stored rows already contain this analysis; no SQL runs again.",
        additivity_note=additivity_note,
        reuse_dimensions=reuse_dimensions,
    )
