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
_MEMORY_WORD = re.compile(r"[a-z0-9_]+")


class SessionMemoryTurn(BaseModel):
    """Compact historical reference; summaries are not database authority."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    result_id: str | None = None
    chart_id: str | None = None
    question: str
    assistant_summary: str
    operation: str
    accepted: bool
    sql_available: bool
    columns: list[str] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    contract_summary: dict[str, Any] = Field(default_factory=dict)
    created_at: float


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
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
        self._memory_epochs: dict[str, float] = {}
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
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
                "CREATE TABLE IF NOT EXISTS session_memory_epochs ("
                "session_id TEXT PRIMARY KEY, reset_at REAL NOT NULL)"
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

    def mark_memory_reset(self, session_id: str) -> None:
        """Start a new memory epoch without deleting the audit history."""
        reset_at = time.time()
        with self._lock:
            if self.path is None:
                self._memory_epochs[session_id] = reset_at
            else:
                with self._connect() as connection:
                    connection.execute(
                        "INSERT INTO session_memory_epochs(session_id, reset_at) VALUES (?, ?) "
                        "ON CONFLICT(session_id) DO UPDATE SET reset_at = excluded.reset_at",
                        (session_id, reset_at),
                    )

    def recent_memory(
        self, session_id: str, db_id: str, *, limit: int = 3,
        parent_run_id: str | None = None,
    ) -> list[SessionMemoryTurn]:
        """Return small, same-session/database references, never SQL or result rows."""
        if parent_run_id:
            return self._lineage_memory(session_id, db_id, parent_run_id)[:limit]
        return self._memory_candidates(session_id, db_id, limit=min(max(limit, 0), 5))

    def search_memory(
        self, session_id: str, db_id: str, question: str, *, limit: int = 3,
        parent_run_id: str | None = None,
    ) -> list[SessionMemoryTurn]:
        """Search historical references only when the Top layer calls the tool."""
        candidates = (
            self._lineage_memory(session_id, db_id, parent_run_id)
            if parent_run_id else self._memory_candidates(session_id, db_id, limit=100)
        )
        if not candidates:
            return []
        bounded = min(max(limit, 1), 5)
        query = question.casefold()
        accepted = [item for item in candidates if item.accepted and item.sql_available]
        if "first" in query and any(word in query for word in ("query", "analysis", "result")):
            return accepted[-1:][:bounded]
        if "previous" in query or "last" in query:
            return candidates[:1]
        terms = set(_MEMORY_WORD.findall(query)) - {
            "the", "this", "that", "with", "from", "show", "compare", "earlier", "result",
        }
        ranked = sorted(
            enumerate(candidates),
            key=lambda pair: (
                -len(terms & set(_MEMORY_WORD.findall(pair[1].question.casefold()))),
                pair[0],
            ),
        )
        return [
            item for _, item in ranked[:bounded]
            if terms & set(_MEMORY_WORD.findall(item.question.casefold()))
        ]

    def _lineage_memory(
        self, session_id: str, db_id: str, run_id: str
    ) -> list[SessionMemoryTurn]:
        root = self.get(run_id, session_id)
        if root is None or root.db_id != db_id:
            return []
        return [
            _memory_from_record(record)
            for record in reversed(self.lineage(session_id, run_id))
            if record.db_id == db_id
        ]

    def _memory_candidates(
        self, session_id: str, db_id: str, *, limit: int
    ) -> list[SessionMemoryTurn]:
        if limit < 1:
            return []
        with self._lock:
            if self.path is None:
                cutoff = self._memory_epochs.get(session_id, 0.0)
                records = [
                    item for item in self._sessions
                    if item.session_id == session_id and item.db_id == db_id
                    and item.created_at > cutoff
                ][:limit]
                return [_memory_from_record(item) for item in records]
            with self._connect() as connection:
                boundary = connection.execute(
                    "SELECT reset_at FROM session_memory_epochs WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                cutoff = float(boundary[0]) if boundary else 0.0
                rows = connection.execute(
                    "SELECT run_id, created_at, "
                    "json_extract(record_json, '$.original_message'), "
                    "json_extract(record_json, '$.response_payload.message'), "
                    "json_extract(record_json, '$.operation'), "
                    "json_extract(record_json, '$.accepted'), "
                    "json_extract(record_json, '$.sql'), "
                    "json_extract(record_json, '$.response_payload.result_id'), "
                    "json_extract(record_json, '$.response_payload.chart_id'), "
                    "json_extract(record_json, '$.columns'), "
                    "json_extract(record_json, '$.row_count'), "
                    "json_extract(record_json, '$.truncated'), "
                    "json_extract(record_json, '$.preview_truncated'), "
                    "json_extract(record_json, '$.state_snapshot.semantic_contract') "
                    "FROM query_runs WHERE session_id = ? "
                    "AND json_extract(record_json, '$.db_id') = ? AND created_at > ? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (session_id, db_id, cutoff, limit),
                ).fetchall()
        return [_memory_from_projection(row) for row in rows]

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
                    connection.execute(
                        "DELETE FROM session_memory_epochs WHERE session_id = ?", (session_id,)
                    )
            self._memory_epochs.pop(session_id, None)
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


def _contract_summary(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    if not isinstance(raw, dict):
        return {}
    return {
        key: raw[key]
        for key in ("measures", "filters", "grain", "active_period", "comparison_periods")
        if raw.get(key)
    }


def _memory_from_record(record: RunRecord) -> SessionMemoryTurn:
    payload = record.response_payload
    return SessionMemoryTurn(
        run_id=record.run_id,
        result_id=payload.get("result_id"),
        chart_id=payload.get("chart_id"),
        question=record.original_message[:300],
        assistant_summary=str(payload.get("message") or record.presentation_summary or "")[:400],
        operation=record.operation,
        accepted=record.accepted,
        sql_available=bool(record.accepted and record.sql),
        columns=record.columns[:12],
        row_count=record.row_count,
        truncated=record.truncated or record.preview_truncated,
        contract_summary=_contract_summary(
            record.state_snapshot.semantic_contract.model_dump(mode="json")
            if record.state_snapshot and record.state_snapshot.semantic_contract else None
        ),
        created_at=record.created_at,
    )


def _memory_from_projection(row: tuple[Any, ...]) -> SessionMemoryTurn:
    columns = json.loads(row[9]) if isinstance(row[9], str) else []
    return SessionMemoryTurn(
        run_id=str(row[0]),
        created_at=float(row[1]),
        question=str(row[2] or "")[:300],
        assistant_summary=str(row[3] or "")[:400],
        operation=str(row[4] or ""),
        accepted=bool(row[5]),
        sql_available=bool(row[5] and row[6]),
        result_id=str(row[7]) if row[7] else None,
        chart_id=str(row[8]) if row[8] else None,
        columns=columns[:12] if isinstance(columns, list) else [],
        row_count=int(row[10] or 0),
        truncated=bool(row[11] or row[12]),
        contract_summary=_contract_summary(row[13]),
    )


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
    if generation:
        payload["generation"]["rows"] = payload["generation"]["rows"][:MAX_PREVIEW_ROWS]
    # History reopens the real response, including failures, attempts, chart and context.
    # A bounded preview is clearly distinguished from the original returned row count.
    return RunRecord(
        run_id=response.run_id or uuid4().hex,
        turn_id=response.turn_id or response.run_id or uuid4().hex,
        session_id=response.session_id,
        parent_run_id=request.parent_run_id if request else None,
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
        rows_preview=list(generation.rows[:MAX_PREVIEW_ROWS]) if generation else [],
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
        preview_truncated=bool(generation and len(generation.rows) > MAX_PREVIEW_ROWS),
    )
