from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from test_dashboard import snapshot

from semantic_text2sql.ai_dashboard_designer import (
    DashboardRevisions,
    dashboard_timeout,
    design_context,
    design_dashboard,
    eligible_catalog,
)
from semantic_text2sql.dashboard_service import DashboardError, DashboardRequest
from semantic_text2sql.models import TokenUsage
from semantic_text2sql.runtime import RequestBudget, active_request_budget


def test_local_designer_timeout_honors_shared_deadline_and_configuration(monkeypatch):
    monkeypatch.delenv("TEXT2SQL_DASHBOARD_TIMEOUT_SECONDS", raising=False)
    budget = RequestBudget(180)
    assert dashboard_timeout("ollama", budget) == 120
    assert dashboard_timeout("groq", budget) == 12
    monkeypatch.setenv("TEXT2SQL_DASHBOARD_TIMEOUT_SECONDS", "90")
    assert dashboard_timeout("ollama", budget) == 90
    short = RequestBudget(3)
    assert 0 < dashboard_timeout("ollama", short) <= 3
    monkeypatch.setenv("TEXT2SQL_DASHBOARD_TIMEOUT_SECONDS", "nan")
    with pytest.raises(ValueError, match="positive finite"):
        dashboard_timeout("ollama", budget)


class Designer:
    def __init__(self, proposal):
        self.proposal = proposal
        self.calls = 0

    async def complete_task(self, model, prompt, task, *, json_mode):
        self.calls += 1
        assert task == "dashboard" and json_mode
        assert "kpi_catalog" in prompt and "chart_catalog" in prompt
        return json.dumps(self.proposal), TokenUsage(input_tokens=100, output_tokens=30)


def setup():
    source = snapshot()
    request = DashboardRequest(session_id="owner", db_id="sales", result_id="result")
    base, kpis, charts = eligible_catalog(source, request)
    return source, request, base, kpis, charts


def run(proposal, *, previous=None, completer=None):
    source, request, *_ = setup()
    provider = completer or Designer(proposal)
    budget = RequestBudget(timeout_seconds=2, max_model_calls=5, max_database_calls=0)
    result = asyncio.run(design_dashboard(
        source=source, request=request, completer=provider, provider="test", model="script",
        budget=budget, previous=previous,
    ))
    return result, provider, budget


def test_ai_selects_real_candidates_width_theme_and_keeps_source():
    source, _, _, kpis, charts = setup()
    chart = next(iter(charts))
    outcome, model, budget = run({
        "result_id": "result", "title": "Revenue overview", "theme": "contrast",
        "kpis": [{"kpi_id": next(iter(kpis)), "column_span": 6}],
        "charts": [{"candidate_id": chart, "column_span": 8}],
    })
    assert outcome.status == "READY" and outcome.spec
    assert outcome.spec.theme == "contrast"
    assert outcome.spec.widgets[0].column_span == 8
    assert outcome.spec.widgets[0].chart_spec.data == charts[chart].chart_spec.data
    assert outcome.spec.result_id == source.result_id
    assert budget.model_calls == model.calls == len(outcome.ledger) == 1
    assert budget.database_calls == 0


def test_incomplete_design_requests_evidence_instead_of_rendering():
    _, _, _, _, charts = setup()
    outcome, model, budget = run({
        "result_id": "result", "charts": [{"candidate_id": next(iter(charts))}],
        "missing_requirements": "Need comparable previous-year data",
    })
    assert outcome.status == "REQUERY_REQUIRED" and outcome.spec is None
    assert outcome.reason
    assert model.calls == budget.model_calls == 1
    assert budget.database_calls == 0


def test_optional_future_analyses_do_not_force_an_unrequested_requery():
    _, _, _, _, charts = setup()
    outcome, model, budget = run({
        "result_id": "result", "charts": [{"candidate_id": next(iter(charts))}],
        "further_analyses": ["Optionally inspect spending by country later"],
    })
    assert outcome.status == "READY" and outcome.spec
    assert model.calls == budget.model_calls == 1 and budget.database_calls == 0


@pytest.mark.parametrize("bad", [
    {"kpis": [{"kpi_id": "invented"}]},
    {"result_id": "another-result"},
    {"title": "<script>alert()</script>"},
    {"charts": [{"candidate_id": "unknown"}]},
])
def test_invalid_proposal_falls_back_without_retries(bad):
    outcome, model, budget = run({"result_id": "result", **bad})
    assert outcome.status == "FALLBACK" and outcome.spec
    assert outcome.spec.result_id == "result"
    assert model.calls == budget.model_calls == 1
    assert outcome.ledger[0].status == "FAILED"


def test_axis_invention_rejected_and_invalid_patch_keeps_previous():
    _, _, _, _, charts = setup()
    key = next(iter(charts))
    first, _, _ = run({"result_id": "result", "charts": [{"candidate_id": key}]})
    assert first.spec
    invalid, _, _ = run({"result_id": "result", "mode": "PATCH", "charts": [
        {"candidate_id": key, "x": "imaginary_column"}
    ]}, previous=first.spec)
    assert invalid.status == "FALLBACK" and invalid.spec
    assert invalid.spec.widgets == first.spec.widgets
    replacement, _, _ = run({"result_id": "result", "charts": []}, previous=first.spec)
    assert replacement.status == "FALLBACK" and replacement.spec
    assert replacement.spec.widgets == first.spec.widgets
    renamed, _, _ = run({"result_id": "result", "charts": [
        {"candidate_id": key, "y_label": "Profit"}
    ]})
    assert renamed.status == "FALLBACK"


def test_patch_preserves_others_and_undo_is_scoped():
    _, _, _, kpis, charts = setup()
    first, _, _ = run({"result_id": "result", "kpis": [{"kpi_id": next(iter(kpis))}],
                      "charts": [{"candidate_id": next(iter(charts))}]})
    assert first.spec
    revisions = DashboardRevisions()
    saved = revisions.commit("owner", first.spec, None)
    patch, _, _ = run({"result_id": "result", "mode": "PATCH", "theme": "neutral"},
                      previous=saved)
    assert patch.spec and patch.spec.widgets == saved.widgets and patch.spec.kpis == saved.kpis
    revisions.commit("owner", patch.spec, saved.revision_id)
    assert revisions.undo("owner", "sales", "result") == saved
    assert revisions.get("other-session", "sales", "result") is None
    with pytest.raises(DashboardError):
        revisions.commit("owner", patch.spec, "stale-revision")


def test_requery_only_reports_missing_data_and_never_executes_sql():
    result, model, budget = run({"result_id": "result", "mode": "REQUERY_REQUIRED",
                                "missing_requirements": "Profit is not present in this result."})
    assert result.status == "REQUERY_REQUIRED" and result.spec is None
    assert model.calls == 1 and budget.database_calls == 0


def test_bounded_context_and_incomplete_source_rejected_before_model():
    source, request, base, kpis, charts = setup()
    context = design_context(source, base, kpis, charts, None)
    assert len(context["sample"]) <= 3
    assert all("data" not in chart for chart in context["chart_catalog"])
    model = Designer({})
    with pytest.raises(DashboardError):
        asyncio.run(design_dashboard(source=replace(source, truncated=True), request=request,
            completer=model, provider="test", model="test", budget=RequestBudget(2)))
    assert model.calls == 0


def test_designer_receives_bounded_distribution_and_relationship_evidence():
    source, _, base, *_ = setup()
    source = replace(source, columns=["x", "y", "constant", "customer_id"],
                     rows=[[1, 2, 7, 10], [2, 4, 7, 11], [4, 8, 7, 12],
                           [1000, 2000, 7, 13]], chart_spec=None)
    context = design_context(source, base, {}, {}, None)
    profiles = {item["column"]: item for item in context["column_profiles"]}
    assert profiles["x"]["distribution"]["iqr_outlier_count"] == 1
    assert profiles["x"]["distribution"]["q25"] == 1.75
    assert profiles["constant"]["distribution"]["stddev_population"] == 0
    assert "distribution" not in profiles["customer_id"]
    relationship = next(item for item in context["relationship_evidence"]
                        if item["x"] == "x" and item["y"] == "y")
    assert relationship["pearson_r"] == 1 and relationship["paired_rows"] == 4
    assert all(item["pearson_r"] is None for item in context["relationship_evidence"]
               if item["y"] == "constant")
    assert len(context["relationship_evidence"]) <= 6
    assert context["sample"] == [] and "returned rows only" in context["evidence_scope"]
    with pytest.raises(DashboardError):
        design_context(replace(source, truncated=True), base, {}, {}, None)


def test_statistical_context_excludes_nonfinite_values_and_reports_missingness():
    source, _, base, *_ = setup()
    source = replace(source, columns=["measure"], rows=[[1.0], [float("nan")],
                     [float("inf")], [None]], chart_spec=None)
    context = design_context(source, base, {}, {}, None)
    profile = context["column_profiles"][0]
    assert profile["non_finite_count"] == 2 and profile["nulls"] == 1
    assert profile["distribution"]["count"] == 1
    assert profile["distribution"]["mean"] == 1
    assert context["relationship_evidence"] == []
    json.dumps(context, allow_nan=False)


def test_same_schema_different_behavior_can_support_different_model_selections():
    class EvidenceDesigner(Designer):
        async def complete_task(self, model, prompt, task, **kwargs):
            self.calls += 1
            context = json.loads(prompt.split(
                "Verified context (profiles/sample are bounded, not the entire result): "
            )[1])
            assert "LAYER 1" in prompt and "LAYER 2" in prompt and "LAYER 3" in prompt
            assert "NOT mandatory sections" in prompt
            distribution = next(p["distribution"] for p in context["column_profiles"]
                                if p["column"] == "revenue")
            candidate = context["chart_catalog"][1 if distribution["iqr_outlier_count"] else 0]
            return json.dumps({"result_id": context["result_id"],
                               "charts": [{"candidate_id": candidate["candidate_id"]}]
                               }), TokenUsage()

    source, request, *_ = setup()
    changed = replace(source, result_id="different-result", chart_spec=None,
                      rows=[[row[0], row[1], value] for row, value
                            in zip(source.rows, [0, 0, 0, 1000], strict=True)])
    selected = []
    for item in (source, changed):
        model = EvidenceDesigner({})
        budget = RequestBudget(2, max_model_calls=1, max_database_calls=0)
        outcome = asyncio.run(design_dashboard(
            source=item, request=request.model_copy(update={"result_id": item.result_id}),
            completer=model, provider="test", model="script", budget=budget,
        ))
        assert outcome.status == "READY" and outcome.spec
        assert len(outcome.spec.widgets) == 1  # No four-perspective quota.
        assert not outcome.spec.kpis  # No KPI quota either.
        selected.append(outcome.spec.widgets[0].chart_spec.chart_type)
        assert budget.model_calls == model.calls == 1 and budget.database_calls == 0
    assert selected[0] != selected[1]


def test_filter_metadata_selection_preserves_actual_scope_and_kpi_definitions():
    from semantic_text2sql.ai_dashboard_designer import DashboardProposal, _materialize
    from semantic_text2sql.dashboard_service import DashboardFilterMetadata

    source, _, base, kpis, charts = setup()
    metadata = DashboardFilterMetadata(field="country", source="stored_result", result_id="result")
    base = base.model_copy(update={"verified_filter_metadata": [metadata]})
    key = next(iter(kpis))
    proposal = DashboardProposal(result_id="result", filter_refs=["filter-0"],
                                 kpis=[{"kpi_id": key}])
    outcome = _materialize(proposal, base, kpis, charts, None)
    assert outcome.verified_filter_metadata == [metadata]
    assert outcome.applied_constraints == base.applied_constraints
    assert outcome.kpis[0].definition == kpis[key].definition
    context = design_context(source, base, kpis, charts, None)
    assert context["kpi_catalog"][0]["definition"] == kpis[key].definition
    invalid = proposal.model_copy(update={"filter_refs": ["unknown"]})
    with pytest.raises(ValueError, match="Unknown verified filter"):
        _materialize(invalid, base, kpis, charts, None)


def test_patch_replacements_removals_order_and_width_preserve_other_widgets():
    _, _, _, kpis, charts = setup()
    ids = list(charts)
    keys = list(kpis)
    first, *_ = run({"result_id": "result", "charts": [
        {"candidate_id": ids[0]}, {"candidate_id": ids[1]}],
        "kpis": [{"kpi_id": keys[0]}, {"kpi_id": keys[1]}]})
    assert first.spec
    previous = first.spec
    patch, *_ = run({"result_id": "result", "mode": "PATCH",
                     "remove_ids": [keys[0]],
                     "kpis": [{"kpi_id": keys[2]}],
                     "charts": [{"candidate_id": ids[2], "replaces_widget_id": ids[0],
                                 "column_span": 12, "order": 1},
                                {"candidate_id": ids[1], "order": 0}]}, previous=previous)
    assert patch.status == "READY" and patch.spec
    assert patch.spec.widgets[0].widget_id == ids[1]
    assert patch.spec.widgets[0].chart_spec == previous.widgets[1].chart_spec
    assert patch.spec.widgets[1].widget_id == ids[0]  # Stable replacement identity.
    assert patch.spec.widgets[1].column_span == 12
    assert patch.spec.kpis[0] == previous.kpis[1]
    assert patch.spec.result_id == previous.result_id
    invalid, *_ = run({"result_id": "result", "mode": "PATCH", "charts": [
        {"candidate_id": ids[0], "order": 0}, {"candidate_id": ids[1], "order": 0}
    ]}, previous=previous)
    assert invalid.status == "FALLBACK" and invalid.spec.widgets == previous.widgets


def test_provider_retry_cannot_exceed_one_specialist_call():
    class Retrying(Designer):
        async def complete_task(self, *args, **kwargs):
            self.calls += 1
            active_request_budget().claim("model")
            raise AssertionError("retry should not reach the provider")
    result, model, budget = run({}, completer=Retrying({}))
    assert result.status == "FALLBACK"
    assert budget.model_calls == model.calls == 1


def test_datasets_offer_different_eligible_metrics_and_charts():
    source, request, _, _, charts = setup()
    scalar = replace(source, rows=[[42]], columns=["count"], row_count=1,
                     sql="SELECT COUNT(*) AS count FROM sales")
    _, metrics, scalar_charts = eligible_catalog(scalar, request)
    assert len(metrics) == 1 and next(iter(metrics.values())).calculation == "DIRECT"
    assert set(charts) != set(scalar_charts)


def test_top_layer_routes_dashboard_with_one_specialist_and_no_sql(registry, tmp_path):
    from test_conversational_agent import Completer, _agent

    from semantic_text2sql.models import ChatRequest, ChatResponse

    source, request, _, _, charts = setup()
    decisions = Completer([{"action": "CALL_TOOL", "intent": "VISUALIZE",
        "tool": "ai_dashboard_designer",
        "tool_arguments": {"objective": "Design a professional dashboard"}}])
    sql_calls = []
    agent = _agent(registry, tmp_path, decisions, sql_calls)
    agent.get_last_response = lambda *_: source
    specialist = Designer({"result_id": "result", "charts": [
        {"candidate_id": next(iter(charts))}]})

    async def handler(chat, objective, current, undo):
        assert current.result_id == source.result_id and not undo
        outcome = await design_dashboard(source=current, request=request, completer=specialist,
            provider="test", model="script", budget=active_request_budget())
        assert outcome.spec
        return ChatResponse(session_id=chat.session_id, operation="VISUALIZE",
            result_id=source.result_id, generation=source.generation,
            resolved_question=objective, message="Dashboard ready.",
            dashboard=outcome.spec.model_dump(), token_usage=outcome.usage,
            call_ledger=list(outcome.ledger))

    agent.dashboard_handler = handler
    response = asyncio.run(agent.run(ChatRequest(session_id="owner", db_id="sales",
                                                message="Design a dashboard from this data")))
    assert response.dashboard and response.result_id == source.result_id
    assert decisions.calls == specialist.calls == 1
    assert sql_calls == []
    assert response.token_usage.input_tokens == 110


def test_ai_api_reuses_real_sql_result_and_undo_without_database_calls(registry, monkeypatch):
    from fastapi.testclient import TestClient

    from semantic_text2sql.agent import TextToSQLAgent
    from semantic_text2sql.api import create_app

    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    monkeypatch.setenv("TEXT2SQL_CONVERSATIONAL_AGENT_ENABLED", "false")
    monkeypatch.setenv("TEXT2SQL_CONTEXT_MODE", "retrieval")
    monkeypatch.setenv("TEXT2SQL_DASHBOARD_PROVIDER", "agentrouter")
    monkeypatch.setenv("TEXT2SQL_DASHBOARD_MODEL", "script")

    class SQL:
        async def generate(self, **kwargs):
            return "SELECT status, SUM(amount) AS total FROM orders GROUP BY status", 1

    class LiveDesigner:
        calls = 0

        async def complete_task(self, model, prompt, task, **kwargs):
            self.calls += 1
            context = json.loads(prompt.split("Verified context (profiles/sample are bounded, "
                                             "not the entire result): ")[1])
            return json.dumps({"result_id": context["result_id"], "theme": "neutral",
                "kpis": [{"kpi_id": context["kpi_catalog"][0]["kpi_id"]}],
                "charts": [{"candidate_id": context["chart_catalog"][0]["candidate_id"]}]
            }), TokenUsage(input_tokens=100, output_tokens=30)

    designer = LiveDesigner()
    model = SQL()
    client = TestClient(create_app(TextToSQLAgent(registry, model),
                                  conversation_completers={"agentrouter": designer}))
    accepted = client.post("/api/chat", json={"session_id": "owner", "db_id": "shop",
        "provider": "agentrouter", "model": "script", "message": "Sum amount by status"}).json()
    assert accepted["generation"]["accepted"], accepted

    def forbidden(*args, **kwargs):
        raise AssertionError("Dashboard design cannot execute SQL or inspect the database")

    monkeypatch.setattr(registry, "execute", forbidden)
    monkeypatch.setattr(registry, "inspect", forbidden)
    monkeypatch.setattr(model, "generate", forbidden)
    payload = {"session_id": "owner", "db_id": "shop", "result_id": accepted["result_id"]}
    initial = client.post("/api/dashboards", json=payload)
    assert initial.status_code == 200
    designed = client.post("/api/dashboards", json={**payload, "design_mode": "ai"})
    assert designed.status_code == 200 and designed.json()["design_mode"] == "ai", designed.text
    assert designed.json()["result_id"] == accepted["result_id"]
    assert designer.calls == 1
    undo = client.post("/api/dashboards/undo", json=payload)
    assert undo.json()["revision_id"] == initial.json()["revision_id"]
    assert client.post("/api/dashboards", json={**payload, "session_id": "stranger",
                                              "design_mode": "ai"}).status_code == 404
    assert designer.calls == 1


def test_phase_two_local_build_benchmark():
    from statistics import median
    from time import perf_counter

    from semantic_text2sql.dashboard_service import build_dashboard

    source, request, _, kpis, charts = setup()
    proposal = {"result_id": "result", "kpis": [{"kpi_id": next(iter(kpis))}],
                "charts": [{"candidate_id": next(iter(charts))}]}
    timings = {"phase1": [], "phase2_scripted": []}
    for _ in range(30):
        started = perf_counter()
        build_dashboard(request, lambda *_: source)
        timings["phase1"].append((perf_counter() - started) * 1000)
        started = perf_counter()
        result = asyncio.run(design_dashboard(source=source, request=request,
            completer=Designer(proposal), provider="script", model="script",
            budget=RequestBudget(2, max_database_calls=0)))
        timings["phase2_scripted"].append((perf_counter() - started) * 1000)
        assert result.status == "READY"
    for label, values in timings.items():
        print(f"{label} 4 rows: p50={median(values):.3f}ms p95={sorted(values)[28]:.3f}ms")


def test_dashboard_missing_data_continues_through_top_sql_then_design(registry, tmp_path):
    from test_conversational_agent import Completer, _agent

    from semantic_text2sql.models import ChatRequest, ChatResponse
    from semantic_text2sql.result_cache import RecentResult

    decisions = Completer([
        {"action": "CALL_TOOL", "intent": "VISUALIZE", "tool": "ai_dashboard_designer",
         "tool_arguments": {"objective": "Design a dashboard with the requested count"}},
        {"action": "CALL_TOOL", "intent": "MODIFY_QUERY", "tool": "ask_data",
         "tool_arguments": {"objective": "Get the requested count", "operation": "REFINE"}},
    ])
    sql_calls, sources = [], []
    agent = _agent(registry, tmp_path, decisions, sql_calls)
    agent.get_last_response = lambda *_: replace(snapshot(), db_id="shop")

    async def handler(chat, objective, current, undo):
        sources.append(current.result_id)
        if len(sources) == 1:
            return ChatResponse(session_id=chat.session_id, operation="VISUALIZE",
                resolved_question=objective, message="The requested count requires new data.")
        retained = RecentResult.from_response(current)
        req = DashboardRequest(session_id=chat.session_id, db_id=chat.db_id,
                               result_id=retained.result_id)
        _, metrics, _ = eligible_catalog(retained, req)
        outcome = await design_dashboard(source=retained, request=req,
            completer=Designer({"result_id": retained.result_id,
                "kpis": [{"kpi_id": next(iter(metrics))}]}), provider="test", model="script",
            budget=active_request_budget())
        assert outcome.spec
        return ChatResponse(session_id=chat.session_id, operation="VISUALIZE",
            resolved_question=objective, result_id=retained.result_id,
            generation=retained.generation, dashboard=outcome.spec.model_dump(),
            message="Dashboard ready.", token_usage=outcome.usage, call_ledger=list(outcome.ledger))

    agent.dashboard_handler = handler
    response = asyncio.run(agent.run(ChatRequest(session_id="owner", db_id="shop",
                                                message="Design a dashboard from this data")))
    assert response.dashboard and decisions.calls == 2
    assert len(sql_calls) == 1 and len(sources) == 2 and sources[0] != sources[1]
    assert response.dashboard["result_id"] == sources[-1]
