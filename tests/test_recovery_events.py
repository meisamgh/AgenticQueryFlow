from __future__ import annotations

from semantic_text2sql.models import (
    Attempt,
    GenerateResponse,
    RecoveryClaim,
    RecoveryToolCall,
    RecoveryTrace,
    StrategyHints,
    ValidationResult,
)
from semantic_text2sql.recovery_events import recovery_events


def test_recovery_events_link_actual_attempts_observations_and_decision() -> None:
    generation = GenerateResponse(
        db_id="test",
        question="Count customers",
        provider="agentrouter",
        model="test",
        strategy=StrategyHints(mode="exact"),
        accepted=True,
        sql="SELECT COUNT(*) FROM customers",
        termination_reason="accepted",
        execution_status="EXECUTABLE",
        attempts=[
            Attempt(
                number=1,
                sql="SELECT wrong FROM customers",
                normalized_sql="",
                validation=ValidationResult(
                    valid=False, code="DATABASE_ERROR", message="Unknown column: wrong"
                ),
                latency_ms=1,
            ),
            Attempt(
                number=2,
                sql="SELECT COUNT(*) FROM customers",
                normalized_sql="",
                validation=ValidationResult(
                    valid=True, code="SQL_SAFETY_VALID", message="Read-only"
                ),
                latency_ms=1,
            ),
        ],
        recovery=RecoveryTrace(
            failure_code="DATABASE_ERROR",
            failure_category="SCHEMA",
            agent_action="REPAIR",
            repair_instruction="Use the inspected columns.",
            diagnosis_summary="Inspected schema.",
            tool_calls=[
                RecoveryToolCall(
                    tool="inspect_schema",
                    purpose="Check available columns",
                    result_summary="Returned metadata.",
                    evidence_id="obs-1",
                )
            ],
            evidence=["obs-1 inspect_schema: {'columns': ['id']}"],
            claims=[
                RecoveryClaim(
                    claim_type="SCHEMA_FACT", statement="id exists.", evidence_ids=["obs-1"]
                )
            ],
        ),
    )
    events = recovery_events(generation, "turn-1")
    assert [item.event_type for item in events] == [
        "SQL_FAILED",
        "SCHEMA_INSPECTED",
        "CLAIM_REPORTED",
        "RECOVERY_DECISION",
        "SQL_EXECUTED",
    ]
    assert events[1].evidence_ids == ["obs-1"]
    assert events[1].detail == generation.recovery.evidence[0]  # type: ignore[union-attr]
    assert events[2].status == "REPORTED"
    assert events[3].status == "REPAIR"
    assert events[-1].sql == generation.sql
    assert events == recovery_events(generation, "turn-1")


def test_inform_or_escalate_without_sql_repair_never_fabricates_execution() -> None:
    for action in ("INFORM", "ESCALATE"):
        generation = GenerateResponse.model_validate(
            {
                "db_id": "test",
                "question": "Unknown customer",
                "provider": "agentrouter",
                "model": "test",
                "strategy": {"mode": "exact"},
                "accepted": False,
                "sql": None,
                "termination_reason": "attempt_limit",
                "recovery": {
                    "failure_code": "ZERO_RESULT",
                    "failure_category": "FILTER",
                    "mode": "ZERO_RESULT",
                    "agent_action": action,
                    "diagnosis_summary": "No matching customer was found.",
                },
            }
        )
        events = recovery_events(generation, "turn-2")
        assert len(events) == 1
        assert events[0].status == action
        assert events[0].event_type == "RECOVERY_DECISION"
        assert events[0].evidence_ids == []
