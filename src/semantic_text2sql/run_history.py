"""Small persistent history of completed query runs for conversational reopening.

Runs are stored with a bounded row preview only; unrestricted result sets,
credentials and connection strings are never written. The store supports the
frontend's session reopening and rerun affordances without rerunning anything.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from semantic_text2sql.models import ConversationState, PresentationState, RecoveryTrace

MAX_PREVIEW_ROWS = 20
DEFAULT_SESSION_HISTORY_LIMIT = 50
GLOBAL_RETENTION_LIMIT = 500


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    result_id: str | None = None
    execution_timestamp: float | None = None
    session_id: str
    conversation_id: str = ""
    turn_id: str = ""
    parent_run_id: str | None = None
    branch_from_parent: bool = False
    sql_version: int = 0
    db_id: str
    operation: str
    original_message: str = Field(max_length=4_000)
    resolved_question: str = Field(max_length=8_000)
    provider: str = Field(max_length=40)
    model: str = Field(max_length=200)
    accepted: bool
    execution_status: str = Field(max_length=30)
    outcome: str = ""
    sql: str | None = Field(default=None, max_length=20_000)
    columns: list[str] = Field(default_factory=list)
    rows_preview: list[list[Any]] = Field(default_factory=list)
    row_count: int = Field(default=0, ge=0)
    truncated: bool = False
    latency_ms: int = Field(default=0, ge=0)
    turn_count: int = Field(default=0, ge=0)
    version: int = Field(default=1, ge=1)
    created_at: float
    token_usage: dict[str, Any] = Field(default_factory=dict)
    timings_ms: dict[str, int] = Field(default_factory=dict)
    recovery: RecoveryTrace | None = None
    presentation_summary: str | None = Field(default=None, max_length=1_800)
    presentation_state: PresentationState | None = None
    state_snapshot: ConversationState | None = None
    response_payload: dict[str, Any] = Field(default_factory=dict)
    preview_truncated: bool = False


class RunHistoryStore:
    """Versionless, bounded append-only store; one record per completed run."""

    def __init__(self, path: Path | None) -> None:
        self._lock = Lock()
        self.path = path
        self._sessions: list[RunRecord] = []
        self._by_id: dict[str, RunRecord] = {}
        self._reset_after: dict[str, float] = {}
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS history_reset_boundaries "
                "(session_id TEXT PRIMARY KEY, reset_at REAL NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS query_runs ("
                "run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
                "created_at REAL NOT NULL, version INTEGER NOT NULL, "
                "record_json TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS query_runs_session "
                "ON query_runs(session_id, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS query_runs_session_db "
                "ON query_runs(session_id, json_extract(record_json, '$.db_id'), created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS query_runs_sql_memory ON query_runs "
                "(session_id, json_extract(record_json, '$.db_id'), created_at, version) "
                "WHERE json_extract(record_json, '$.accepted') = 1 "
                "AND json_extract(record_json, '$.sql') IS NOT NULL"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS query_runs_result_identity ON query_runs "
                "(session_id, json_extract(record_json, '$.db_id'), "
                "COALESCE(json_extract(record_json, '$.result_id'), "
                "json_extract(record_json, '$.response_payload.result_id')))"
            )

    @classmethod
    def from_environment(cls) -> RunHistoryStore:
        raw = os.environ.get("TEXT2SQL_RUN_HISTORY_PATH", "data/runtime/query_history.sqlite")
        return cls(None if raw == ":memory:" else Path(raw))

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(cast_path(self.path))

    def record(self, entry: RunRecord) -> RunRecord:
        with self._lock:
            if self.path is not None:
                with sqlite3.connect(cast_path(self.path), isolation_level=None) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    # Decoration needs only the latest version and one parent,
                    # not hundreds of complete response payloads.
                    latest = connection.execute(
                        "SELECT record_json FROM query_runs WHERE session_id = ? "
                        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                        (entry.session_id,),
                    ).fetchone()
                    parent = (
                        connection.execute(
                            "SELECT record_json FROM query_runs WHERE session_id = ? "
                            "AND run_id = ? LIMIT 1",
                            (entry.session_id, entry.parent_run_id),
                        ).fetchone()
                        if entry.parent_run_id
                        else connection.execute(
                            "SELECT record_json FROM query_runs WHERE session_id = ? "
                            "AND json_extract(record_json, '$.db_id') = ? "
                            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                            (entry.session_id, entry.db_id),
                        ).fetchone()
                    )
                    recent: list[RunRecord] = []
                    for row in (latest, parent):
                        if row is not None:
                            record = RunRecord.model_validate_json(row[0])
                            if not recent or record.run_id != recent[0].run_id:
                                recent.append(record)
                    entry = self._decorate(entry, recent)
                    connection.execute(
                        "INSERT INTO query_runs(run_id, session_id, created_at, "
                        "version, record_json) VALUES (?, ?, ?, ?, ?)",
                        (
                            entry.run_id,
                            entry.session_id,
                            entry.created_at,
                            entry.version,
                            entry.model_dump_json(),
                        ),
                    )
                    connection.execute(
                        "DELETE FROM query_runs WHERE run_id NOT IN ("
                        "SELECT run_id FROM query_runs "
                        "ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                        (GLOBAL_RETENTION_LIMIT,),
                    )
                    connection.commit()
                return entry
            entry = self._decorate(entry, self._list_unlocked(entry.session_id))
            self._by_id[entry.run_id] = entry
            self._sessions.append(entry)
            self._sessions.sort(key=lambda item: (item.created_at, item.version), reverse=True)
            self._prune()
            return entry

    @staticmethod
    def _decorate(entry: RunRecord, recent: list[RunRecord]) -> RunRecord:
        parent = next((item for item in recent if item.run_id == entry.parent_run_id), None)
        if entry.parent_run_id and (parent is None or parent.db_id != entry.db_id):
            raise ValueError("The parent turn is unavailable for this session and database.")
        if parent is None and entry.operation != "NEW_QUERY":
            parent = next((item for item in recent if item.db_id == entry.db_id), None)
        if entry.operation == "NEW_QUERY" and not entry.branch_from_parent:
            parent = None
        conversation_id = (
            uuid4().hex
            if entry.branch_from_parent and parent
            else (parent.conversation_id or parent.run_id)
            if parent
            else uuid4().hex
        )
        sql_version = parent.sql_version if parent else 0
        previous_sql = (
            parent.state_snapshot.last_sql
            if parent and parent.state_snapshot
            else parent.sql
            if parent
            else None
        )
        if entry.accepted and entry.sql and entry.sql != previous_sql:
            sql_version += 1
        record = entry.model_copy(
            deep=True,
            update={
                "run_id": entry.run_id or uuid4().hex,
                "turn_id": entry.turn_id or entry.run_id or uuid4().hex,
                "conversation_id": conversation_id,
                "parent_run_id": parent.run_id if parent else None,
                "version": (recent[0].version + 1) if recent else 1,
                "sql_version": sql_version,
            },
        )
        record.response_payload.update(
            {
                "run_id": record.run_id,
                "turn_id": record.turn_id,
                "conversation_id": record.conversation_id,
                "parent_run_id": record.parent_run_id,
                "sql_version": record.sql_version,
                "history_saved": True,
                "presentation_state": (
                    record.presentation_state.model_dump(mode="json")
                    if record.presentation_state
                    else None
                ),
            }
        )
        return record

    def list_sessions(
        self, session_id: str, limit: int = DEFAULT_SESSION_HISTORY_LIMIT
    ) -> list[RunRecord]:
        if limit < 1:
            return []
        if self.path is not None:
            with self._lock, self._connect() as connection:
                rows = connection.execute(
                    "SELECT record_json FROM query_runs WHERE session_id = ? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (session_id, min(limit, GLOBAL_RETENTION_LIMIT)),
                ).fetchall()
        else:
            with self._lock:
                bounded = min(limit, GLOBAL_RETENTION_LIMIT)
                rows = [
                    (item.model_dump_json(),) for item in self._list_unlocked(session_id)[:bounded]
                ]
        records = []
        for (raw,) in rows:
            try:
                records.append(RunRecord.model_validate_json(raw))
            except Exception:
                continue
        return records

    def get(self, run_id: str, session_id: str | None = None) -> RunRecord | None:
        if self.path is not None:
            with self._lock, self._connect() as connection:
                row = connection.execute(
                    "SELECT record_json FROM query_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
            record = RunRecord.model_validate_json(row[0]) if row else None
            return (
                record
                if record and (session_id is None or record.session_id == session_id)
                else None
            )
        with self._lock:
            record = self._by_id.get(run_id)
            return (
                record
                if record and (session_id is None or record.session_id == session_id)
                else None
            )

    def sql_memory(
        self,
        session_id: str,
        db_id: str,
        reference: str,
        *,
        parent_run_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Indexed SQL/intent lookup. Rows never enter conversational memory.

        Canonical references are first, latest, analysis:N (1-based; negative
        from newest), or an exact run/result ID. The LLM selects the reference;
        this is not a natural-language intent classifier.
        """
        position = 1 if reference == "first" else -1 if reference == "latest" else None
        if match := re.fullmatch(r"analysis:(-?[1-9]\d{0,2})", reference):
            position = int(match[1])
        if position is None and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", reference):
            return None
        boundary = self._reset_after.get(session_id, 0.0)
        with self._lock:
            if self.path is not None:
                with self._connect() as connection:
                    reset = connection.execute(
                        "SELECT reset_at FROM history_reset_boundaries WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()
                    boundary = float(reset[0]) if reset else boundary
                    scope = (
                        "session_id = ? AND json_extract(record_json, '$.db_id') = ? "
                        "AND json_extract(record_json, '$.accepted') = 1 "
                        "AND json_extract(record_json, '$.sql') IS NOT NULL "
                        "AND created_at > ? "
                    )
                    params: list[Any] = [session_id, db_id, boundary]
                    if parent_run_id:
                        # Explicit branches may only reference their ancestry.
                        lineage = connection.execute(
                            "WITH RECURSIVE ancestors(id, depth) AS (SELECT ?, 0 UNION ALL "
                            "SELECT json_extract(q.record_json, '$.parent_run_id'), a.depth+1 "
                            "FROM query_runs q JOIN ancestors a ON q.run_id=a.id "
                            "WHERE q.session_id=? AND a.depth<500) "
                            "SELECT id FROM ancestors WHERE id IS NOT NULL",
                            (parent_run_id, session_id),
                        ).fetchall()
                        ids = [row[0] for row in lineage]
                        scope += "AND run_id IN (" + ",".join("?" for _ in ids) + ") "
                        params.extend(ids)
                    row = None
                    if position is None:
                        row = connection.execute(
                            "SELECT record_json FROM query_runs WHERE " + scope + "AND run_id=?",
                            (*params, reference),
                        ).fetchone()
                        if row is None:
                            row = connection.execute(
                                "SELECT record_json FROM query_runs WHERE "
                                + scope
                                + "AND COALESCE(json_extract(record_json, '$.result_id'), "
                                "json_extract(record_json, '$.response_payload.result_id')) = ? "
                                "ORDER BY created_at, version LIMIT 1",
                                (*params, reference),
                            ).fetchone()
                    else:
                        direction = "ASC" if position > 0 else "DESC"
                        row = connection.execute(
                            "SELECT record_json FROM query_runs WHERE "
                            + scope
                            + "AND json_extract(record_json, '$.operation') != 'VISUALIZE' "
                            f"ORDER BY created_at {direction}, version {direction} "
                            "LIMIT 1 OFFSET ?",
                            (*params, abs(position) - 1),
                        ).fetchone()
                    record = RunRecord.model_validate_json(row[0]) if row else None
            else:
                records = [
                    item
                    for item in self._sessions
                    if item.session_id == session_id
                    and item.db_id == db_id
                    and item.accepted
                    and item.sql
                    and item.created_at > boundary
                ]
                if parent_run_id:
                    ancestor_ids: set[str] = set()
                    cursor = self._by_id.get(parent_run_id)
                    while (
                        cursor
                        and cursor.session_id == session_id
                        and cursor.run_id not in ancestor_ids
                    ):
                        ancestor_ids.add(cursor.run_id)
                        cursor = self._by_id.get(cursor.parent_run_id or "")
                    records = [item for item in records if item.run_id in ancestor_ids]
                if position is None:
                    record = next(
                        (
                            item
                            for item in reversed(records)
                            if reference
                            in {
                                item.run_id,
                                item.result_id,
                                item.response_payload.get("result_id"),
                            }
                        ),
                        None,
                    )
                else:
                    records = sorted(
                        (r for r in records if r.operation != "VISUALIZE"),
                        key=lambda r: (r.created_at, r.version),
                    )
                    index = position - 1 if position > 0 else position
                    record = records[index] if -len(records) <= index < len(records) else None
        if record is None:
            return None
        return {
            "run_id": record.run_id,
            "result_id": record.result_id
            or record.response_payload.get("result_id")
            or record.run_id,
            "db_id": record.db_id,
            "original_question": record.original_message,
            "resolved_question": record.resolved_question,
            "sql": record.sql,
            "semantic_contract": record.state_snapshot.semantic_contract.model_dump(mode="json")
            if record.state_snapshot and record.state_snapshot.semantic_contract
            else None,
            "execution_timestamp": record.execution_timestamp or record.created_at,
            "timestamp_source": "execution" if record.execution_timestamp else "legacy_record_time",
            "evidence_kind": "SQL_DEFINITION_ONLY",
            "verified_numerical_evidence": False,
            "snapshot_available": not record.truncated
            and not record.preview_truncated
            and record.execution_status not in {"NOT_EXECUTED", "SAFETY_VALIDATED"}
            and record.row_count == len(record.rows_preview),
            "provenance": (
                "Accepted historical SQL; refreshed execution is current data, not old figures."
            ),
        }

    def inspect_result(
        self,
        session_id: str,
        db_id: str,
        reference: str,
        *,
        offset: int = 0,
        page_size: int = 20,
        conversation_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Read retained evidence only; never restore state or execute saved SQL."""
        if offset < 0 or not 1 <= page_size <= 100:
            raise ValueError("Invalid historical page bounds.")
        records = self._inspection_records(session_id)
        boundary = self._reset_after.get(session_id, 0.0)
        if self.path is not None:
            with self._lock, self._connect() as connection:
                row = connection.execute(
                    "SELECT reset_at FROM history_reset_boundaries WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                boundary = float(row[0]) if row else boundary
        record = next(
            (
                item
                for item in records
                if item.db_id == db_id
                and item.accepted
                and item.created_at > boundary
                and (not conversation_id or item.conversation_id == conversation_id)
                and reference in {item.run_id, item.response_payload.get("result_id")}
            ),
            None,
        )
        if record is None:
            return None
        complete = not record.truncated and record.row_count == len(record.rows_preview)
        state = record.state_snapshot
        return {
            "run_id": record.run_id,
            "result_id": record.response_payload.get("result_id") or record.run_id,
            "historical": True,
            "question": record.original_message,
            "resolved_question": record.resolved_question,
            "sql": record.sql,
            "answer": record.presentation_summary,
            "columns": record.columns,
            "rows": record.rows_preview[offset : offset + page_size],
            "row_count": record.row_count,
            "retained_row_count": len(record.rows_preview),
            "complete": complete,
            "truncated": record.truncated,
            "calculation_allowed": complete and offset == 0 and page_size >= record.row_count,
            "offset": offset,
            "page_complete": complete and offset == 0 and page_size >= record.row_count,
            "has_more_retained": offset + page_size < len(record.rows_preview),
            "semantic_contract": state.semantic_contract.model_dump(mode="json")
            if state and state.semantic_contract
            else None,
            "warning": None
            if complete
            else ("Retained preview only; do not calculate full-result totals or comparisons."),
            "created_at": record.created_at,
        }

    def _inspection_records(self, session_id: str) -> list[RunRecord]:
        return self.list_sessions(session_id, GLOBAL_RETENTION_LIMIT)

    def mark_memory_reset(self, session_id: str) -> None:
        """Invalidate inspection references, preserving audit records."""
        boundary = time.time()
        with self._lock:
            self._reset_after[session_id] = boundary
            if self.path is not None:
                with self._connect() as connection:
                    connection.execute(
                        "INSERT OR REPLACE INTO history_reset_boundaries VALUES (?, ?)",
                        (session_id, boundary),
                    )

    def conversation(self, session_id: str, conversation_id: str) -> list[RunRecord]:
        return list(
            reversed(
                [
                    item
                    for item in self.list_sessions(session_id, GLOBAL_RETENTION_LIMIT)
                    if (item.conversation_id or item.run_id) == conversation_id
                ]
            )
        )

    def lineage(self, session_id: str, run_id: str) -> list[RunRecord]:
        available = {
            item.run_id: item for item in self.list_sessions(session_id, GLOBAL_RETENTION_LIMIT)
        }
        chain: list[RunRecord] = []
        seen: set[str] = set()
        while run_id in available and run_id not in seen:
            seen.add(run_id)
            record = available[run_id]
            chain.append(record)
            run_id = record.parent_run_id or ""
        return list(reversed(chain))

    def drop_session(self, session_id: str) -> None:
        with self._lock:
            if self.path is not None:
                with self._connect() as connection:
                    connection.execute("DELETE FROM query_runs WHERE session_id = ?", (session_id,))
            self._sessions = [item for item in self._sessions if item.session_id != session_id]
            self._by_id = {
                key: item for key, item in self._by_id.items() if item.session_id != session_id
            }

    def _list_unlocked(self, session_id: str) -> list[RunRecord]:
        if self.path is not None:
            with sqlite3.connect(cast_path(self.path)) as connection:
                rows = connection.execute(
                    "SELECT record_json FROM query_runs WHERE session_id = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (session_id, DEFAULT_SESSION_HISTORY_LIMIT),
                ).fetchall()
            records = []
            for (raw,) in rows:
                try:
                    records.append(RunRecord.model_validate_json(raw))
                except Exception:
                    continue
            return records
        return sorted(
            [item for item in self._sessions if item.session_id == session_id],
            key=lambda item: (item.created_at, item.version),
            reverse=True,
        )

    def _prune(self) -> None:
        overflowing = len(self._sessions) - GLOBAL_RETENTION_LIMIT
        if overflowing > 0:
            stale = self._sessions[-overflowing:]
            self._sessions = self._sessions[:-overflowing]
            for item in stale:
                self._by_id.pop(item.run_id, None)


def cast_path(path: Path | None) -> str:
    return str(path) if path is not None else ":memory:"


def now() -> float:
    return time.time()


def build_run_record(raw: dict[str, Any]) -> RunRecord:
    """Build a record from a completed ChatResponse plus the original request."""
    response = raw["response"]
    generation = response.generation
    request = raw.get("request")
    state = response.state
    db_id = generation.db_id if generation else getattr(request, "db_id", "")
    if state and state.db_id != db_id:
        state = None
    if state is None and generation is not None:
        # A failed first query still needs its own question for later corrections.
        # Do not restore a different database's accepted SQL or invent successful rows.
        state = ConversationState(
            session_id=response.session_id,
            db_id=db_id,
            root_question=generation.question,
            resolved_question=response.resolved_question,
            last_failure=response.message if not generation.accepted else None,
            last_failed_sql=(
                generation.attempts[-1].sql
                if generation.attempts and not generation.accepted
                else None
            ),
        )
    payload = response.model_dump(mode="json")
    preview_limit = max(0, min(int(os.environ.get("TEXT2SQL_HISTORY_PREVIEW_ROWS", "20")), 20))
    if generation:
        payload["generation"]["rows"] = payload["generation"]["rows"][:preview_limit]
    chart = (payload.get("presentation") or {}).get("chart_spec")
    if chart and isinstance(chart.get("data"), list):
        chart["data"] = chart["data"][:preview_limit]
    # History reopens the real response, including failures, attempts, chart and context.
    # A bounded preview is clearly distinguished from the original returned row count.
    return RunRecord(
        run_id=response.run_id or uuid4().hex,
        result_id=response.result_id,
        execution_timestamp=generation.execution_timestamp if generation else None,
        turn_id=response.turn_id or response.run_id or uuid4().hex,
        session_id=response.session_id,
        parent_run_id=response.parent_run_id or (request.parent_run_id if request else None),
        branch_from_parent=request.branch_from_parent if request else False,
        db_id=db_id,
        operation=response.operation,
        original_message=raw["message"],
        resolved_question=response.resolved_question,
        provider=generation.provider if generation else getattr(request, "provider", "agentrouter"),
        model=generation.model if generation else getattr(request, "model", ""),
        accepted=generation.accepted if generation else False,
        execution_status=generation.execution_status if generation else "NOT_EXECUTED",
        outcome=(
            "NEEDS_CLARIFICATION"
            if response.clarification_required or response.human_review
            else "EXPLANATION"
            if response.operation.startswith("EXPLAIN")
            else "EXECUTED"
            if generation and generation.accepted
            else "FAILED"
        ),
        sql=generation.sql if generation else None,
        columns=list(generation.columns) if generation else [],
        rows_preview=list(generation.rows[:preview_limit]) if generation else [],
        row_count=generation.row_count if generation else 0,
        truncated=generation.truncated if generation else False,
        latency_ms=int(raw.get("latency_ms", 0)),
        turn_count=(response.state.turn_count if response.state else 0),
        created_at=now(),
        token_usage=json.loads(response.token_usage.model_dump_json()),
        timings_ms=dict(response.timings_ms or {}),
        recovery=generation.recovery if generation else None,
        presentation_summary=(response.presentation.summary if response.presentation else None),
        presentation_state=response.presentation_state,
        response_payload=payload,
        state_snapshot=state,
        preview_truncated=bool(generation and len(generation.rows) > preview_limit),
    )
