"""Deterministic result presentation and explicit visualization planning."""

from __future__ import annotations

from typing import Any

from semantic_text2sql.models import CallLedgerEntry, ResultPresentation, TokenUsage
from semantic_text2sql.runtime import RequestBudget
from semantic_text2sql.visualization import (
    VisualizationError,
    VisualizationIntent,
    plan_chart,
)


async def present_result(
    completer: Any,
    *,
    question: str,
    sql: str,
    columns: list[str],
    rows: list[list[Any]],
    truncated: bool,
    model: str,
    provider: str,
    budget: RequestBudget,
    visualization: VisualizationIntent | None = None,
) -> tuple[ResultPresentation, TokenUsage, list[CallLedgerEntry]]:
    """Present exact returned data without an additional explanation-model call.

    The Top-layer assistant owns conversational explanation. The charting model
    explains its selected visual when a chart is requested.
    """
    usage = TokenUsage()
    ledger: list[CallLedgerEntry] = []
    note = "Based on returned rows only; not independent verification of correctness."
    if truncated:
        note = "The executed result is truncated; inspect the available rows only."
    result = ResultPresentation(
        summary=f"The query returned {len(rows)} rows. View the result table for the exact values.",
        source="fallback",
        note=note,
    )
    if len(rows) == 1:
        result.summary = "; ".join(
            f"{column.replace('_', ' ')}: {value if value is not None else 'not available'}"
            for column, value in zip(columns, rows[0], strict=True)
        )[:1800]
    if visualization and visualization.requested:
        try:
            spec = plan_chart(
                visualization,
                question=question,
                columns=columns,
                rows=rows,
                truncated=truncated,
            )
            result.chart_type = spec.chart_type
            result.chart_spec = spec
            result.x, result.y, result.title = spec.x, spec.y, spec.title
            result.x_label, result.y_label = spec.x_label, spec.y_label
            result.number_format, result.time_format = spec.number_format, spec.time_format
        except VisualizationError as exc:
            result.chart_reason = str(exc)

    return result, usage, ledger
