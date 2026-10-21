"""Presentation events derived exclusively from recorded SQL attempts and recovery observations."""

from __future__ import annotations

from semantic_text2sql.models import GenerateResponse, RecoveryEvent


def recovery_events(generation: GenerateResponse | None, turn_id: str) -> list[RecoveryEvent]:
    if generation is None or generation.recovery is None:
        return []
    trace = generation.recovery
    if not trace.activated:
        return []
    events: list[RecoveryEvent] = []

    def add(**fields: object) -> None:
        events.append(
            RecoveryEvent.model_validate(
                {
                    "event_id": f"{turn_id}-event-{len(events) + 1}",
                    **fields,
                }
            )
        )

    def attempt_event(index: int) -> None:
        attempt = generation.attempts[index]
        if not attempt.validation.valid:
            add(
                event_type="SQL_FAILED",
                status="FAILED",
                attempt=attempt.number,
                summary=attempt.validation.message[:1_000],
                sql=attempt.sql,
                detail=attempt.validation.code,
            )
        elif generation.execution_status in {"EXECUTABLE", "ACCEPTED"}:
            add(
                event_type="SQL_EXECUTED",
                status="EXECUTED",
                attempt=attempt.number,
                summary="The selected SQL passed safety validation and executed.",
                sql=attempt.sql,
            )

    if trace.mode == "FAILURE" and generation.attempts:
        attempt_event(0)
    else:
        for index in range(len(generation.attempts)):
            attempt_event(index)

    tool_types = {
        "inspect_schema": "SCHEMA_INSPECTED",
        "inspect_values": "VALUES_INSPECTED",
        "query_database": "DATABASE_PROBED",
        "run_probe": "DATABASE_PROBED",
    }
    for call in trace.tool_calls:
        # Preserve the original evidence ID. A tool observation is not itself a proof
        # that a later causal/business claim is correct.
        detail = next(
            (
                item
                for item in trace.evidence
                if call.evidence_id and item.startswith(call.evidence_id + " ")
            ),
            None,
        )
        add(
            event_type=tool_types.get(call.tool, "TOOL_OBSERVED"),
            status="OBSERVED",
            summary=(call.purpose or call.result_summary)[:1_000],
            tool=call.tool,
            evidence_ids=[call.evidence_id] if call.evidence_id else [],
            detail=(detail or call.result_summary)[:12_000],
        )
    for claim in trace.claims:
        add(
            event_type="CLAIM_REPORTED",
            status="REPORTED",
            summary=claim.statement,
            detail=claim.claim_type,
            evidence_ids=claim.evidence_ids,
        )
    if trace.agent_action:
        add(
            event_type="RECOVERY_DECISION",
            status=trace.agent_action,
            summary=(
                trace.diagnosis_summary
                or trace.agent_diagnosis
                or f"Recovery chose {trace.agent_action.lower()}."
            )[:1_000],
            detail=trace.repair_instruction,
        )
    if trace.mode == "FAILURE":
        for index in range(1, len(generation.attempts)):
            attempt_event(index)
    return events[:40]
