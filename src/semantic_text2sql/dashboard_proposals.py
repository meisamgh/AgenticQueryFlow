"""Versioned, session-scoped design discussions in the existing Studio database.

The retained document is a validated candidate, not a constructed/published dashboard.
Only an explicit approved version may be handed to the rendering/Studio path.
"""

from __future__ import annotations

from time import time
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from semantic_text2sql.ai_dashboard_designer import DashboardProposal
from semantic_text2sql.dashboard_service import DashboardError
from semantic_text2sql.dashboard_store import DashboardDocument, DashboardStore
from semantic_text2sql.models import StrictModel


class DesignItem(StrictModel):
    widget_id: str
    kind: Literal["KPI", "CHART"]
    title: str
    analytical_objective: str
    reason: str
    result_id: str
    chart_type: str | None = None
    column_span: int
    source_requirements: list[str] = Field(default_factory=list)


class DesignSource(StrictModel):
    result_id: str
    analytical_objective: str
    row_count: int
    execution_timestamp: float | None
    evidence_mode: Literal["RETAINED_SNAPSHOT"] = "RETAINED_SNAPSHOT"


class ProposedLayout(StrictModel):
    columns: Literal[12] = 12
    section_order: tuple[
        Literal["filters"], Literal["kpis"], Literal["charts"], Literal["data"]
    ] = ("filters", "kpis", "charts", "data")
    widget_order: list[str] = Field(default_factory=list, max_length=8)


class DashboardDesignProposal(StrictModel):
    proposal_id: str = Field(default_factory=lambda: uuid4().hex)
    version: int = 1
    status: Literal[
        "INVESTIGATING",
        "PROPOSED",
        "AWAITING_APPROVAL",
        "REVISING",
        "APPROVED",
        "BUILDING",
        "READY",
        "REJECTED",
        "EXPIRED",
        "FAILED",
    ]
    objective: str
    items: list[DesignItem] = Field(default_factory=list, max_length=8)
    supported_findings: list[str] = Field(default_factory=list, max_length=12)
    missing_requirements: list[str] = Field(default_factory=list, max_length=8)
    created_at: float = Field(default_factory=time)
    expires_at: float
    # Never returned as conversational context: contains private retained evidence.
    document: DashboardDocument
    base_revision_id: str | None = None
    saved_dashboard_id: str | None = None
    saved_version: int | None = None
    discussion: list[str] = Field(default_factory=list, max_length=20)
    approval_statement: str | None = Field(default=None, max_length=1000)
    investigation_evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=8)
    source_requirements: list[DesignSource] = Field(default_factory=list, max_length=4)
    layout: ProposedLayout = Field(default_factory=ProposedLayout)
    theme: Literal["green", "neutral", "contrast"] = "green"

    def summary(self) -> dict[str, Any]:
        compact = self.model_dump(exclude={"document"})
        compact["discussion"] = [message[:400] for message in self.discussion[-4:]]
        compact["supported_findings"] = self.supported_findings[:6]
        for source in compact["source_requirements"]:
            source["analytical_objective"] = source["analytical_objective"][:500]
        compact["investigation_evidence"] = [
            {**item, "columns": dict(list(item.get("columns", {}).items())[:6])}
            for item in self.investigation_evidence[-2:]
        ]
        return compact


class ProposalStore:
    def __init__(self, studio: DashboardStore):
        self.studio = studio
        with studio._transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS dashboard_design_proposals ("
                "session_id TEXT NOT NULL, db_id TEXT NOT NULL, version INTEGER NOT NULL, "
                "payload TEXT NOT NULL, PRIMARY KEY(session_id,db_id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS dashboard_design_proposal_history ("
                "session_id TEXT NOT NULL, db_id TEXT NOT NULL, version INTEGER NOT NULL, "
                "payload TEXT NOT NULL, PRIMARY KEY(session_id,db_id,version))"
            )
            db.execute("INSERT OR IGNORE INTO dashboard_studio_migrations(version) VALUES(3)")

    def get(self, session: str, database: str) -> DashboardDesignProposal | None:
        with self.studio._transaction() as db:
            row = db.execute(
                "SELECT payload FROM dashboard_design_proposals WHERE session_id=? AND db_id=?",
                (session, database),
            ).fetchone()
        return DashboardDesignProposal.model_validate_json(row[0]) if row else None

    def clear(self, session: str) -> None:
        with self.studio._transaction() as db:
            db.execute("DELETE FROM dashboard_design_proposals WHERE session_id=?", (session,))
            db.execute(
                "DELETE FROM dashboard_design_proposal_history WHERE session_id=?", (session,)
            )

    def save_candidate(
        self, session: str, database: str, proposal: DashboardDesignProposal, expected: int | None
    ) -> DashboardDesignProposal:
        """Record all discussion stages atomically; never expose a half-written design."""
        proposal = DashboardDesignProposal.model_validate(proposal.model_dump())
        proposal.document.verify()
        if proposal.document.source.db_id != database:
            raise DashboardError("The proposed evidence belongs to another database.")
        version = expected or 0
        stages = (
            ("REVISING", "PROPOSED", "AWAITING_APPROVAL")
            if expected
            else ("INVESTIGATING", "PROPOSED", "AWAITING_APPROVAL")
        )
        history = []
        for status in stages:
            version += 1
            proposal = proposal.model_copy(update={"status": status, "version": version})
            payload = proposal.model_dump_json()
            if len(payload.encode()) > 4_000_000:
                raise DashboardError("The proposal exceeds the retained evidence limit.")
            history.append((session, database, version, payload))
        with self.studio._transaction() as db:
            if expected is None:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO dashboard_design_proposals VALUES(?,?,?,?)",
                    (session, database, version, payload),
                )
            else:
                cursor = db.execute(
                    "UPDATE dashboard_design_proposals SET version=?,payload=? "
                    "WHERE session_id=? AND db_id=? AND version=?",
                    (version, payload, session, database, expected),
                )
            if cursor.rowcount != 1:
                raise DashboardError("The design changed in another request. Review it again.", 409)
            db.executemany("INSERT INTO dashboard_design_proposal_history VALUES(?,?,?,?)", history)
        return proposal

    def put(
        self, session: str, database: str, proposal: DashboardDesignProposal, expected: int | None
    ) -> DashboardDesignProposal:
        proposal.document.verify()
        if proposal.document.source.db_id != database:
            raise DashboardError("The proposed evidence belongs to another database.")
        updated = DashboardDesignProposal.model_validate(
            {
                **proposal.model_dump(),
                "version": (expected or 0) + 1,
            }
        )
        payload = updated.model_dump_json()
        if len(payload.encode()) > 4_000_000:
            raise DashboardError("The proposal exceeds the retained evidence limit.")
        with self.studio._transaction() as db:
            if expected is None:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO dashboard_design_proposals VALUES(?,?,?,?)",
                    (session, database, updated.version, payload),
                )
            else:
                cursor = db.execute(
                    "UPDATE dashboard_design_proposals SET version=?,payload=? "
                    "WHERE session_id=? AND db_id=? AND version=?",
                    (updated.version, payload, session, database, expected),
                )
            if cursor.rowcount != 1:
                raise DashboardError("The design changed in another request. Review it again.", 409)
            db.execute(
                "INSERT INTO dashboard_design_proposal_history VALUES(?,?,?,?)",
                (session, database, updated.version, payload),
            )
        return updated

    def transition(
        self,
        session: str,
        database: str,
        identity: str,
        expected: int,
        status: str,
        statement: str | None = None,
    ) -> DashboardDesignProposal:
        proposal = self.get(session, database)
        if not proposal or proposal.proposal_id != identity or proposal.version != expected:
            raise DashboardError(
                "The requested design version is unavailable. Review it again.", 409
            )
        allowed = {
            "AWAITING_APPROVAL": {"APPROVED", "REJECTED", "REVISING", "EXPIRED"},
            "APPROVED": {"BUILDING", "EXPIRED"},
            "BUILDING": {"READY", "FAILED"},
            "FAILED": {"APPROVED", "REVISING", "EXPIRED"},
            "READY": {"REVISING"},
            "REJECTED": {"REVISING"},
            "EXPIRED": {"REVISING"},
        }
        if status not in allowed.get(proposal.status, set()):
            raise DashboardError("This design is not ready for that action.", 409)
        if status == "APPROVED" and not (statement and statement.strip()):
            raise DashboardError("Explicit client confirmation is required.", 409)
        if status == "APPROVED" and not proposal.items and not proposal.missing_requirements:
            raise DashboardError("The design has no supported KPIs or charts. Revise it.", 409)
        if status == "BUILDING" and proposal.missing_requirements:
            raise DashboardError("The approved design still requires analytical evidence.", 409)
        if status in {"APPROVED", "BUILDING"} and proposal.expires_at <= time():
            raise DashboardError(
                "The design evidence has expired. Request a revised proposal.", 409
            )
        changes: dict[str, Any] = {"status": status}
        if status == "APPROVED":
            changes["approval_statement"] = statement
        return self.put(session, database, proposal.model_copy(update=changes), expected)


def propose(
    document: DashboardDocument,
    objective: str,
    rationale: str,
    previous: DashboardDesignProposal | None = None,
    choices: DashboardProposal | None = None,
) -> DashboardDesignProposal:
    document.verify()
    items = [
        DesignItem(
            widget_id=k.kpi_id,
            kind="KPI",
            title=k.title,
            analytical_objective=k.title,
            reason=f"Verified {k.calculation} metric.",
            result_id=k.result_id,
            column_span=k.column_span,
        )
        for k in document.spec.kpis
    ]
    items += [
        DesignItem(
            widget_id=w.widget_id,
            kind="CHART",
            title=w.chart_spec.title,
            analytical_objective=w.chart_spec.title,
            reason=rationale,
            chart_type=w.chart_spec.chart_type,
            result_id=w.result_id,
            column_span=w.column_span,
        )
        for w in document.spec.widgets
    ]
    if choices:
        reasoning = {choice.replaces_widget_id or choice.kpi_id: choice for choice in choices.kpis}
        chart_reasoning = {
            choice.replaces_widget_id or choice.candidate_id: choice for choice in choices.charts
        }
        for item in items:
            selected = (
                reasoning.get(item.widget_id)
                if item.kind == "KPI"
                else chart_reasoning.get(item.widget_id)
            )
            if selected:
                item.reason = selected.reason or item.reason
                item.analytical_objective = (
                    selected.analytical_objective or item.analytical_objective
                )
    if previous:
        old_items = {item.widget_id: item for item in previous.items}
        changed_ids = (
            {c.replaces_widget_id or c.kpi_id for c in choices.kpis}
            | {c.replaces_widget_id or c.candidate_id for c in choices.charts}
            if choices
            else set()
        )
        for item in items:
            old = old_items.get(item.widget_id)
            if old and item.widget_id not in changed_ids and old.result_id == item.result_id:
                item.reason, item.analytical_objective = old.reason, old.analytical_objective
    for item in items:
        if item.kind == "CHART":
            chart = next(
                w.chart_spec for w in document.spec.widgets if w.widget_id == item.widget_id
            )
            item.source_requirements = [name for name in (chart.x, chart.y, chart.series) if name]
        else:
            item.source_requirements = [
                next(k.metric for k in document.spec.kpis if k.kpi_id == item.widget_id)
            ]
    return DashboardDesignProposal(
        proposal_id=previous.proposal_id if previous else uuid4().hex,
        status="AWAITING_APPROVAL",
        objective=objective,
        layout=ProposedLayout(widget_order=[item.widget_id for item in items]),
        theme=document.spec.theme,
        items=items,
        document=document,
        supported_findings=["The proposed mappings use complete, validated analytical results."],
        missing_requirements=choices.further_analyses if choices else [],
        source_requirements=[
            DesignSource(
                result_id=item.result_id,
                analytical_objective=item.question,
                row_count=item.row_count,
                execution_timestamp=item.execution_timestamp,
            )
            for item in document.results().values()
        ],
        expires_at=time() + 86400,
    )


def proposal_message(proposal: DashboardDesignProposal) -> str:
    choices = "\n".join(
        f"- {item.title}: {item.chart_type or 'KPI'} — {item.reason}" for item in proposal.items
    )
    return (
        f"Proposed dashboard: {proposal.objective}\n{choices}\n\n"
        + (
            "Evidence reviewed:\n" + "\n".join(proposal.supported_findings[1:4]) + "\n\n"
            if len(proposal.supported_findings) > 1
            else ""
        )
        + (
            "Additional evidence needed:\n" + "\n".join(proposal.missing_requirements) + "\n\n"
            if proposal.missing_requirements
            else ""
        )
        + "Would you like to confirm this design, add more plots, "
        "or modify the proposed visualizations?"
    )
