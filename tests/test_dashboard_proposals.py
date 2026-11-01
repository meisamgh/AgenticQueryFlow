from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from time import time

import pytest
from fastapi.testclient import TestClient
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


@pytest.mark.parametrize("fail_finalization", [False, True])
def test_restart_approval_uses_snapshots_without_sql_or_designer(
    registry, tmp_path, monkeypatch, fail_finalization
):
    from dataclasses import replace

    from test_dashboard import snapshot

    from semantic_text2sql.dashboard_service import DashboardRequest, build_dashboard
    from semantic_text2sql.dashboard_store import DashboardDocument, DashboardSnapshot

    monkeypatch.setenv("TEXT2SQL_ASSISTANT_PROVIDER", "agentrouter")
    monkeypatch.setenv("TEXT2SQL_ASSISTANT_MODEL", "script")
    monkeypatch.setenv("TEXT2SQL_CONVERSATIONAL_AGENT_ENABLED", "true")
    path = tmp_path / "history.sqlite"
    source = replace(snapshot(), db_id="shop")
    spec = build_dashboard(
        DashboardRequest(session_id="owner", db_id="shop", result_id="result"), lambda *_: source
    )
    candidate = propose(
        DashboardDocument(spec=spec, source=DashboardSnapshot.capture(source)),
        "Revenue trend",
        "Compare retained periods.",
    )
    proposal = ProposalStore(DashboardStore(path)).save_candidate("owner", "shop", candidate, None)

    class SQL:
        async def generate(self, **kwargs):
            pytest.fail("Approval must not generate SQL")

    class Top:
        calls = 0

        async def complete_detailed(self, model, prompt):
            self.calls += 1
            context = json.loads(prompt.split("\nContext: ")[1])
            pending = context["current_dashboard"]["pending_proposal"]
            assert pending["version"] >= proposal.version
            return json.dumps(
                {
                    "action": "CALL_TOOL",
                    "intent": "VISUALIZE",
                    "tool": "ai_dashboard_designer",
                    "tool_arguments": {
                        "objective": "Approve retained design",
                        "design_action": "APPROVE",
                        "proposal_id": pending["proposal_id"],
                        "proposal_version": pending["version"],
                        "approval_quote": context["question"],
                    },
                }
            ), TokenUsage()

    def forbidden(*args, **kwargs):
        pytest.fail("Approval must not inspect or execute against the business database")

    model = Top()
    if fail_finalization:
        transition = ProposalStore.transition
        failed = False

        def injected_failure(self, session, database, identity, expected, status, **kwargs):
            nonlocal failed
            if status == "READY" and not failed:
                failed = True
                raise DashboardError("Injected durable finalization failure")
            return transition(self, session, database, identity, expected, status, **kwargs)

        monkeypatch.setattr(ProposalStore, "transition", injected_failure)
    app = create_app(
        TextToSQLAgent(registry, SQL()),
        conversation_completers={"agentrouter": model},
        run_history=RunHistoryStore(path),
    )
    monkeypatch.setattr(registry, "inspect", forbidden)
    monkeypatch.setattr(registry, "execute", forbidden)
    client = TestClient(app)
    response = client.post(
        "/api/chat",
        json={
            "session_id": "owner",
            "db_id": "shop",
            "provider": "agentrouter",
            "model": "script",
            "message": "I confirm this design",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    if fail_finalization:
        assert body["dashboard"] is None
        assert body["dashboard_design_proposal"]["status"] == "FAILED"
        assert (
            ProposalStore(DashboardStore(path)).get("owner", "shop").document == candidate.document
        )
        response = client.post(
            "/api/chat",
            json={
                "session_id": "owner",
                "db_id": "shop",
                "provider": "agentrouter",
                "model": "script",
                "message": "I confirm, please retry",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
    assert body["dashboard"] and body["result_id"] == "result", body
    assert model.calls == (2 if fail_finalization else 1)
    assert ProposalStore(DashboardStore(path)).get("owner", "shop").status == "READY"


@pytest.mark.parametrize("valid", [True, False])
def test_layout_revision_requires_new_confirmation_and_never_runs_sql(
    registry, tmp_path, monkeypatch, valid
):
    from dataclasses import replace

    from test_dashboard import snapshot

    from semantic_text2sql.dashboard_service import DashboardRequest, build_dashboard
    from semantic_text2sql.dashboard_store import DashboardDocument, DashboardSnapshot

    monkeypatch.setenv("TEXT2SQL_ASSISTANT_PROVIDER", "agentrouter")
    monkeypatch.setenv("TEXT2SQL_DASHBOARD_PROVIDER", "agentrouter")
    monkeypatch.setenv("TEXT2SQL_ASSISTANT_MODEL", "script")
    monkeypatch.setenv("TEXT2SQL_CONVERSATIONAL_AGENT_ENABLED", "true")
    path = tmp_path / "history.sqlite"
    source = replace(snapshot(), db_id="shop")
    spec = build_dashboard(
        DashboardRequest(session_id="owner", db_id="shop", result_id="result"), lambda *_: source
    )
    pending = propose(
        DashboardDocument(spec=spec, source=DashboardSnapshot.capture(source)),
        "Revenue performance",
        "Compare observed periods.",
    )
    store = ProposalStore(DashboardStore(path))
    original = store.save_candidate("owner", "shop", pending, None)

    class Model:
        top_calls = 0
        design_calls = 0

        async def complete_detailed(self, model, prompt):
            self.top_calls += 1
            context = json.loads(prompt.split("\nContext: ")[1])
            proposal = context["current_dashboard"]["pending_proposal"]
            return json.dumps(
                {
                    "action": "CALL_TOOL",
                    "intent": "VISUALIZE",
                    "tool": "ai_dashboard_designer",
                    "tool_arguments": {
                        "objective": "Widen the first chart only",
                        "design_action": "REVISE",
                        "proposal_id": proposal["proposal_id"],
                        "proposal_version": proposal["version"],
                    },
                }
            ), TokenUsage()

        async def complete_task(self, model, prompt, task, **kwargs):
            if task != "dashboard":
                return await self.complete_detailed(model, prompt)
            self.design_calls += 1
            return json.dumps(
                {
                    "result_id": "result",
                    "mode": "PATCH",
                    "charts": [
                        {
                            "candidate_id": original.document.spec.widgets[0].widget_id
                            if valid
                            else "invented",
                            "column_span": 12,
                        }
                    ],
                }
            ), TokenUsage()

    class SQL:
        async def generate(self, **kwargs):
            pytest.fail("Layout changes cannot generate SQL")

    def forbidden(*args, **kwargs):
        pytest.fail("Layout changes cannot query or inspect the business database")

    model = Model()
    app = create_app(
        TextToSQLAgent(registry, SQL()),
        conversation_completers={"agentrouter": model},
        run_history=RunHistoryStore(path),
    )
    monkeypatch.setattr(registry, "inspect", forbidden)
    monkeypatch.setattr(registry, "execute", forbidden)
    client = TestClient(app)
    response = client.post(
        "/api/chat",
        json={
            "session_id": "owner",
            "db_id": "shop",
            "provider": "agentrouter",
            "model": "script",
            "message": "Make the first chart wider",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (
        body["dashboard"] is None
        and body["dashboard_design_proposal"]
        and body["dashboard_design_proposal"]["status"] == "AWAITING_APPROVAL"
    ), body
    revised = store.get("owner", "shop")
    if valid:
        assert revised.version == original.version + 3
        assert revised.document.spec.widgets[0].column_span == 12
        assert revised.document.spec.widgets[1:] == original.document.spec.widgets[1:]
        assert revised.document.source == original.document.source
        assert revised.objective == original.objective and model.design_calls == 1
    else:
        assert revised == original and model.design_calls == 2
    assert model.top_calls == 1


def test_design_action_validation_rejects_missing_references_or_approval_quote():
    from pydantic import ValidationError

    from semantic_text2sql.conversational_agent import ToolInput

    for action in ("APPROVE", "REVISE", "REJECT"):
        with pytest.raises(ValidationError):
            ToolInput(objective="Change design", design_action=action)
    with pytest.raises(ValidationError):
        ToolInput(
            objective="Approve design", design_action="APPROVE", proposal_id="p", proposal_version=3
        )
    with pytest.raises(ValidationError, match="require REVISE"):
        ToolInput(
            objective="Approve but add a plot",
            design_action="APPROVE",
            proposal_id="p",
            proposal_version=3,
            approval_quote="Approve but add a plot",
            changes=[{"operation": "CHANGE_VISUALIZATION", "target": "plot", "new_value": "bar"}],
        )


def test_approved_missing_evidence_uses_governed_sql_then_requests_new_mapping_review(
    registry, tmp_path, monkeypatch
):
    from dataclasses import replace

    from test_dashboard import snapshot

    from semantic_text2sql.dashboard_service import DashboardRequest, build_dashboard
    from semantic_text2sql.dashboard_store import DashboardDocument, DashboardSnapshot

    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    for setting in ("ASSISTANT", "DASHBOARD"):
        monkeypatch.setenv(f"TEXT2SQL_{setting}_PROVIDER", "agentrouter")
        monkeypatch.setenv(f"TEXT2SQL_{setting}_MODEL", "script")
    monkeypatch.setenv("TEXT2SQL_CONTEXT_MODE", "retrieval")
    monkeypatch.setenv("TEXT2SQL_CONVERSATIONAL_AGENT_ENABLED", "true")
    path = tmp_path / "history.sqlite"
    source = replace(snapshot(), db_id="shop")
    spec = build_dashboard(
        DashboardRequest(session_id="owner", db_id="shop", result_id="result"), lambda *_: source
    )
    candidate = propose(
        DashboardDocument(spec=spec, source=DashboardSnapshot.capture(source)),
        "Revenue and order status",
        "Compare retained periods.",
    )
    candidate.missing_requirements = ["Count orders by status"]
    ProposalStore(DashboardStore(path)).save_candidate("owner", "shop", candidate, None)

    class SQL:
        calls = 0

        async def generate(self, **kwargs):
            self.calls += 1
            return "SELECT status, COUNT(*) AS orders FROM orders GROUP BY status", 1

    class Model:
        top_calls = 0
        design_calls = 0

        async def complete_detailed(self, model, prompt):
            self.top_calls += 1
            context = json.loads(prompt.split("\nContext: ")[1])
            pending = context["current_dashboard"]["pending_proposal"]
            if pending["status"] == "AWAITING_APPROVAL":
                arguments = {
                    "objective": pending["objective"],
                    "design_action": "APPROVE",
                    "proposal_id": pending["proposal_id"],
                    "proposal_version": pending["version"],
                    "approval_quote": context["question"],
                }
                tool, intent = "ai_dashboard_designer", "VISUALIZE"
            elif not context["dashboard_evidence"]:
                arguments = {
                    "objective": "Count orders by status",
                    "operation": "NEW_QUERY",
                    "dashboard_goal": pending["objective"],
                }
                tool, intent = "ask_data", "NEW_QUERY"
            else:
                arguments = {
                    "objective": "Add the verified order status plot",
                    "design_action": "REVISE",
                    "proposal_id": pending["proposal_id"],
                    "proposal_version": pending["version"],
                    "source_result_ids": ["result", context["dashboard_evidence"][0]["result_id"]],
                }
                tool, intent = "ai_dashboard_designer", "VISUALIZE"
            return json.dumps(
                {"action": "CALL_TOOL", "intent": intent, "tool": tool, "tool_arguments": arguments}
            ), TokenUsage()

        async def complete_task(self, model, prompt, task, **kwargs):
            if task != "dashboard":
                return await self.complete_detailed(model, prompt)
            self.design_calls += 1
            context = json.loads(
                prompt.split(
                    "Verified context (profiles/sample are bounded, not the entire result): "
                )[1]
            )
            chart = next(c for c in context["chart_catalog"] if c["result_id"] != "result")
            return json.dumps(
                {
                    "result_id": context["result_id"],
                    "mode": "PATCH",
                    "charts": [
                        {
                            "candidate_id": chart["candidate_id"],
                            "reason": "Compare the observed order statuses.",
                        }
                    ],
                }
            ), TokenUsage()

    sql, model = SQL(), Model()
    client = TestClient(
        create_app(
            TextToSQLAgent(registry, sql),
            conversation_completers={"agentrouter": model},
            run_history=RunHistoryStore(path),
        )
    )
    payload = {
        "session_id": "owner",
        "db_id": "shop",
        "provider": "agentrouter",
        "model": "script",
        "message": "I confirm; proceed with the proposed analysis",
    }
    response = client.post("/api/chat", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (
        body["dashboard"] is None
        and body["dashboard_design_proposal"]
        and body["dashboard_design_proposal"]["status"] == "AWAITING_APPROVAL"
    ), (body["message"], body["call_ledger"], model.top_calls, sql.calls, model.design_calls)
    assert sql.calls == 1 and model.top_calls == 3 and model.design_calls == 1
    assert len(body["dashboard_design_proposal"]["source_requirements"]) == 2
    assert body["dashboard_design_proposal"]["missing_requirements"] == []
    response = client.post(
        "/api/chat", json={**payload, "message": "I confirm the verified design"}
    )
    assert response.status_code == 200 and response.json()["dashboard"], response.text
    assert sql.calls == 1 and model.top_calls == 4 and model.design_calls == 1
