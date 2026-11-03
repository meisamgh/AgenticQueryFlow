from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from time import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_dashboard_store import document

from semantic_text2sql.agent import TextToSQLAgent
from semantic_text2sql.api import create_app
from semantic_text2sql.dashboard_proposals import ProposalStore, propose
from semantic_text2sql.dashboard_service import DashboardError
from semantic_text2sql.dashboard_store import DashboardPrincipal, DashboardStore
from semantic_text2sql.models import TokenUsage
from semantic_text2sql.run_history import RunHistoryStore


def seed(path, session="owner", database="sales"):
    store = ProposalStore(DashboardStore(path))
    candidate = propose(document(), "Revenue performance", "Compare observed periods.")
    return store, store.save_candidate(session, database, candidate, None)


def test_restart_retains_discussion_and_only_exposes_compact_design(tmp_path):
    path = tmp_path / "history.sqlite"
    store, proposal = seed(path)
    reopened = ProposalStore(DashboardStore(path)).get("owner", "sales")
    assert reopened == proposal
    assert proposal.status == "AWAITING_APPROVAL" and proposal.version == 3
    public = proposal.summary()
    assert "document" not in public and "rows" not in json.dumps(public)
    assert public["items"] and all(i["source_requirements"] for i in public["items"])
    assert public["layout"]["widget_order"] == [item.widget_id for item in proposal.items]
    assert proposal.layout.section_order == ("filters", "kpis", "charts", "data")
    with sqlite3.connect(path) as db:
        states = [
            json.loads(row[0])["status"]
            for row in db.execute(
                "SELECT payload FROM dashboard_design_proposal_history ORDER BY version"
            )
        ]
    assert states == ["INVESTIGATING", "PROPOSED", "AWAITING_APPROVAL"]
    assert DashboardStore(path).list_dashboards(DashboardPrincipal("local-workspace")) == []
    assert store.get("other", "sales") is None and store.get("owner", "other") is None


def test_exact_version_and_approval_are_required(tmp_path):
    store, proposal = seed(tmp_path / "history.sqlite")
    with pytest.raises(DashboardError, match="Explicit client"):
        store.transition("owner", "sales", proposal.proposal_id, proposal.version, "APPROVED")
    with pytest.raises(DashboardError):
        store.transition("owner", "sales", proposal.proposal_id, proposal.version, "BUILDING")
    approved = store.transition(
        "owner", "sales", proposal.proposal_id, proposal.version, "APPROVED", statement="I confirm"
    )
    with pytest.raises(DashboardError):
        store.transition("owner", "sales", proposal.proposal_id, proposal.version, "APPROVED")
    building = store.transition(
        "owner", "sales", approved.proposal_id, approved.version, "BUILDING"
    )
    ready = store.transition("owner", "sales", building.proposal_id, building.version, "READY")
    assert ready.document == proposal.document
    with pytest.raises(DashboardError):
        store.transition("owner", "sales", ready.proposal_id, ready.version, "APPROVED")


def test_revision_invalidates_old_approval_and_retains_unchanged_choices(tmp_path):
    store, proposal = seed(tmp_path / "history.sqlite")
    revised = propose(proposal.document, "Change layout only", "Preserve source data.", proposal)
    revised = store.save_candidate("owner", "sales", revised, proposal.version)
    assert revised.document == proposal.document and revised.status == "AWAITING_APPROVAL"
    assert revised.proposal_id == proposal.proposal_id
    with pytest.raises(DashboardError):
        store.transition("owner", "sales", proposal.proposal_id, proposal.version, "APPROVED")


def test_expired_and_rejected_designs_cannot_build(tmp_path):
    store, proposal = seed(tmp_path / "history.sqlite")
    expired = store.put(
        "owner", "sales", proposal.model_copy(update={"expires_at": time() - 1}), proposal.version
    )
    with pytest.raises(DashboardError, match="expired"):
        store.transition(
            "owner",
            "sales",
            expired.proposal_id,
            expired.version,
            "APPROVED",
            statement="I confirm",
        )
    rejected = store.transition("owner", "sales", expired.proposal_id, expired.version, "REJECTED")
    assert rejected.document == proposal.document
    with pytest.raises(DashboardError):
        store.transition("owner", "sales", rejected.proposal_id, rejected.version, "BUILDING")


def test_empty_design_cannot_be_approved_as_a_completed_dashboard(tmp_path):
    store, proposal = seed(tmp_path / "history.sqlite")
    proposal = store.put("owner", "sales", proposal.model_copy(update={"items": []}),
                         proposal.version)
    with pytest.raises(DashboardError, match="no supported KPIs or charts"):
        store.transition("owner", "sales", proposal.proposal_id, proposal.version,
                         "APPROVED", statement="Confirm")
    assert store.get("owner", "sales").status == "AWAITING_APPROVAL"


def test_failed_build_keeps_approved_evidence_and_history(tmp_path):
    store, proposal = seed(tmp_path / "history.sqlite")
    original = proposal.document
    for status in ("APPROVED", "BUILDING", "FAILED"):
        proposal = store.transition(
            "owner",
            "sales",
            proposal.proposal_id,
            proposal.version,
            status,
            statement="I confirm" if status == "APPROVED" else None,
        )
    assert proposal.document == original
    assert store.get("owner", "sales").status == "FAILED"


def test_missing_analyses_cannot_be_built_even_after_approval(tmp_path):
    store, proposal = seed(tmp_path / "history.sqlite")
    proposal = store.put(
        "owner",
        "sales",
        proposal.model_copy(
            update={"missing_requirements": ["Compare revenue with the previous year"]}
        ),
        proposal.version,
    )
    approved = store.transition(
        "owner",
        "sales",
        proposal.proposal_id,
        proposal.version,
        "APPROVED",
        statement="Please proceed",
    )
    with pytest.raises(DashboardError, match="requires analytical evidence"):
        store.transition("owner", "sales", approved.proposal_id, approved.version, "BUILDING")
    assert store.get("owner", "sales").status == "APPROVED"


def test_concurrent_revisions_have_one_winner_and_reset_is_scoped(tmp_path):
    path = tmp_path / "history.sqlite"
    store, proposal = seed(path)
    store.save_candidate("another", "sales", propose(document(), "Other", "Other"), None)

    def revise(_):
        try:
            ProposalStore(DashboardStore(path)).save_candidate(
                "owner", "sales", proposal, proposal.version
            )
            return True
        except DashboardError:
            return False

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(revise, range(2))) == [False, True]
    store.clear("owner")
    assert store.get("owner", "sales") is None and store.get("another", "sales")


def test_invalid_source_lineage_cannot_replace_a_pending_proposal(tmp_path):
    store, proposal = seed(tmp_path / "history.sqlite")
    payload = proposal.model_dump()
    payload["document"]["spec"]["widgets"][0]["result_id"] = "unrelated-result"
    from pydantic import ValidationError

    from semantic_text2sql.dashboard_proposals import DashboardDesignProposal

    with pytest.raises((DashboardError, ValidationError)):
        invalid = DashboardDesignProposal.model_validate(payload)
        store.save_candidate("owner", "sales", invalid, proposal.version)
    assert store.get("owner", "sales") == proposal


def test_pending_context_is_bounded_without_discarding_persisted_discussion(tmp_path):
    store, proposal = seed(tmp_path / "history.sqlite")
    proposal.discussion = [str(i) + "x" * 799 for i in range(20)]
    saved = store.put("owner", "sales", proposal, proposal.version)
    assert len(store.get("owner", "sales").discussion) == 20
    assert len(saved.summary()["discussion"]) == 4
    assert all(len(message) <= 400 for message in saved.summary()["discussion"])


@pytest.mark.parametrize("action", ["APPROVE", "REJECT"])
def test_legacy_pending_proposals_are_not_used_or_mutated(registry, tmp_path, monkeypatch, action):
    from semantic_text2sql.conversational_agent import ToolInput

    monkeypatch.setenv("TEXT2SQL_ASSISTANT_PROVIDER", "agentrouter")
    monkeypatch.setenv("TEXT2SQL_ASSISTANT_MODEL", "script")
    monkeypatch.setenv("TEXT2SQL_CONVERSATIONAL_AGENT_ENABLED", "true")
    path = tmp_path / "history.sqlite"
    store, proposal = seed(path)

    class SQL:
        async def generate(self, **kwargs):
            pytest.fail("Legacy approval cannot generate SQL")

    class Top:
        async def complete_detailed(self, model, prompt):
            context = json.loads(prompt.split("\nContext: ")[1])
            assert not (context.get("current_dashboard") or {}).get("pending_proposal")
            return json.dumps({
                "action": "CALL_TOOL", "intent": "VISUALIZE", "tool": "ai_dashboard_designer",
                "tool_arguments": ToolInput(
                    objective="Use archived proposal", design_action=action,
                    proposal_id=proposal.proposal_id, proposal_version=proposal.version,
                    approval_quote=context["question"] if action == "APPROVE" else None,
                ).model_dump(),
            }), TokenUsage()

    app = create_app(TextToSQLAgent(registry, SQL()),
                     conversation_completers={"agentrouter": Top()},
                     run_history=RunHistoryStore(path))
    response = TestClient(app).post("/api/chat", json={
        "session_id": "owner", "db_id": "sales", "provider": "agentrouter", "model": "script",
        "message": "Confirm the old design",
    })
    assert response.status_code == 200
    assert response.json()["dashboard"] is None
    assert response.json()["dashboard_design_proposal"] is None
    assert store.get("owner", "sales") == proposal


def test_revise_has_no_approval_dependency_and_legacy_approval_stays_strict():
    from semantic_text2sql.conversational_agent import ToolInput

    assert ToolInput(objective="Improve layout", design_action="REVISE").proposal_id is None
    with pytest.raises(ValidationError):
        ToolInput(objective="Approve", design_action="APPROVE")


@pytest.mark.parametrize("concurrent", [False, True])
def test_missing_evidence_is_requeried_then_dashboard_built_without_confirmation(
    registry, tmp_path, monkeypatch, concurrent
):
    from semantic_text2sql import api
    from semantic_text2sql.ai_dashboard_designer import DashboardRevisions

    # A competing edit finishes while the model is awaited. The handler must
    # retain its pre-await expected revision, not reread and overwrite the winner.
    instances = []
    initialize = DashboardRevisions.__init__

    def capture(self):
        initialize(self)
        instances.append(self)

    monkeypatch.setattr(DashboardRevisions, "__init__", capture)
    actual_design = api.design_dashboard
    winner = None

    async def racing_design(**kwargs):
        nonlocal winner
        outcome = await actual_design(**kwargs)
        if concurrent and outcome.status == "READY":
            spec = outcome.spec.model_copy(update={"title": "Concurrent winner"})
            winner = instances[-1].commit("owner", spec, None)
        return outcome

    monkeypatch.setattr(api, "design_dashboard", racing_design)
    for setting in ("ASSISTANT", "DASHBOARD"):
        monkeypatch.setenv(f"TEXT2SQL_{setting}_PROVIDER", "agentrouter")
        monkeypatch.setenv(f"TEXT2SQL_{setting}_MODEL", "script")
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    monkeypatch.setenv("TEXT2SQL_CONTEXT_MODE", "retrieval")
    monkeypatch.setenv("TEXT2SQL_CONVERSATIONAL_AGENT_ENABLED", "true")
    goal = "Dashboard showing order status and spending by customer"

    class SQL:
        calls = 0
        async def generate(self, **kwargs):
            self.calls += 1
            return (
                "SELECT status, COUNT(*) AS order_count FROM orders GROUP BY status"
                if self.calls == 1 else
                "SELECT customer_id, SUM(amount) AS spending FROM orders GROUP BY customer_id"
            ), 1

    class Model:
        top_calls = 0
        design_calls = 0
        async def complete_detailed(self, model, prompt):
            self.top_calls += 1
            context = json.loads(prompt.split("\nContext: ")[1])
            if self.top_calls in {1, 3}:
                return json.dumps({
                    "action": "CALL_TOOL", "intent": "NEW_QUERY", "tool": "ask_data",
                    "tool_arguments": {
                        "objective": ("Count orders by status" if self.top_calls == 1
                                      else "Sum order amount by customer_id"),
                        "operation": "NEW_QUERY", "dashboard_goal": goal,
                    },
                }), TokenUsage()
            return json.dumps({
                "action": "CALL_TOOL", "intent": "VISUALIZE", "tool": "ai_dashboard_designer",
                "tool_arguments": {"objective": goal, "source_result_ids": [
                    item["result_id"] for item in context["dashboard_evidence"]
                ]},
            }), TokenUsage()

        async def complete_task(self, model, prompt, task, **kwargs):
            if task != "dashboard":
                return await self.complete_detailed(model, prompt)
            self.design_calls += 1
            context = json.loads(prompt.split(
                "Verified context (profiles/sample are bounded, not the entire result): "
            )[1])
            if self.design_calls == 1:
                return json.dumps({"result_id": context["result_id"],
                                   "mode": "REQUERY_REQUIRED",
                                   "missing_requirements": "Sum order amount by customer_id"
                                   }), TokenUsage()
            seen = set()
            charts = []
            for item in context["chart_catalog"]:
                if item["result_id"] not in seen:
                    seen.add(item["result_id"])
                    charts.append({"candidate_id": item["candidate_id"], "column_span": 12,
                                   "analytical_role": "PRIMARY" if len(charts) == 0
                                   else "COMPARISON"})
            return json.dumps({"result_id": context["result_id"], "charts": charts}), TokenUsage()

    sql, model = SQL(), Model()
    client = TestClient(create_app(
        TextToSQLAgent(registry, sql), conversation_completers={"agentrouter": model},
        run_history=RunHistoryStore(tmp_path / "runs.sqlite"),
    ))
    response = client.post("/api/chat", json={
        "session_id": "owner", "db_id": "shop", "provider": "agentrouter",
        "model": "script", "message": goal,
    })
    assert response.status_code == 200, response.text
    body = response.json()
    if concurrent:
        assert body["dashboard"] is None
        assert "concurrently" in body["message"]
        assert instances[-1].get("owner", "shop", winner.result_id) == winner
        assert sql.calls == 2 and model.top_calls == 4 and model.design_calls == 2
        return
    assert body["dashboard"], (body["message"], body["call_ledger"])
    assert body["dashboard_design_proposal"] is None
    assert len(body["dashboard"]["sources"]) == 2
    assert sql.calls == 2 and model.top_calls == 4 and model.design_calls == 2
