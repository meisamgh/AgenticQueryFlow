"""Minimal in-process snapshot of an accepted result for chart follow-ups."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from semantic_text2sql.dialects import DatabaseDialect
from semantic_text2sql.models import (
    ChartSpec,
    ChatResponse,
    GenerateResponse,
    ModelProvider,
    ResultPresentation,
    StrategyHints,
)


@dataclass(frozen=True, slots=True)
class RecentResult:
    run_id: str | None
    result_id: str | None
    chart_id: str | None
    db_id: str
    question: str
    provider: ModelProvider
    model: str
    dialect: DatabaseDialect
    strategy: StrategyHints
    sql: str
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    chart_spec: ChartSpec | None
    summary: str
    provenance: list[str]
    verified_context: dict[str, Any]
    execution_timestamp: float | None = None

    @property
    def message(self) -> str:
        return self.summary

    @classmethod
    def from_response(cls, response: ChatResponse) -> RecentResult:
        generation = response.generation
        if generation is None or not generation.accepted or generation.sql is None:
            raise ValueError("Only accepted, executed results may be cached.")
        return cls(
            run_id=(
                response.presentation_state.source_run_id
                if response.presentation_state and response.presentation_state.mode == "chart"
                else response.run_id
            ),
            result_id=response.result_id,
            execution_timestamp=generation.execution_timestamp,
            chart_id=response.chart_id,
            db_id=generation.db_id,
            question=generation.question,
            provider=generation.provider,
            model=generation.model,
            dialect=generation.dialect,
            strategy=generation.strategy,
            sql=generation.sql,
            columns=generation.columns,
            rows=generation.rows,
            row_count=generation.row_count,
            truncated=generation.truncated,
            chart_spec=response.presentation.chart_spec if response.presentation else None,
            summary=response.presentation.summary if response.presentation else response.message,
            provenance=response.provenance,
            verified_context={
                key: value
                for key, value in (generation.model_context or {}).items()
                if key
                in {
                    "tables",
                    "relationships",
                    "business_context",
                    "optional_metadata",
                    "historical_examples",
                }
            },
        )

    @property
    def generation(self) -> GenerateResponse:
        """Materialize only the accepted rows needed by the existing chart API."""
        return GenerateResponse(
            execution_timestamp=self.execution_timestamp,
            db_id=self.db_id,
            question=self.question,
            provider=self.provider,
            model=self.model,
            dialect=self.dialect,
            strategy=self.strategy,
            sql=self.sql,
            accepted=True,
            execution_status="ACCEPTED",
            rows=self.rows,
            columns=self.columns,
            row_count=self.row_count,
            truncated=self.truncated,
            termination_reason="accepted",
            model_context=self.verified_context or None,
        )

    @property
    def presentation(self) -> ResultPresentation:
        return ResultPresentation(
            summary=self.summary,
            source="fallback",
            chart_spec=self.chart_spec,
            chart_type=self.chart_spec.chart_type if self.chart_spec else "none",
        )
