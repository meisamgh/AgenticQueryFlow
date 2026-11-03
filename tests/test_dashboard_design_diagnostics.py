from __future__ import annotations

import asyncio
import copy
import json
import logging
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from test_ai_dashboard_designer import Designer, run, setup

from semantic_text2sql.agent import TextToSQLAgent
from semantic_text2sql.ai_dashboard_designer import design_dashboard
from semantic_text2sql.api import create_app
from semantic_text2sql.llm import ProviderRequestError
from semantic_text2sql.models import TokenUsage
from semantic_text2sql.runtime import RequestBudget


def test_grounded_years_and_labels_are_not_rejected_as_numerical_findings():
    _, _, _, _, charts = setup()
    outcome, *_ = run(
        {
            "result_id": "result",
            "title": "Revenue 2026",
            "charts": [{"candidate_id": next(iter(charts)), "title": "Monthly revenue 2026"}],
            "rationale": "Compare accepted revenue during 2026.",
        }
    )
    assert outcome.status == "READY" and not outcome.failures


def test_chart_years_are_grounded_in_their_own_source_not_another_result():
    from semantic_text2sql.multi_dashboard import composition_catalog

    source, request, *_ = setup()
    other = replace(
        source,
        result_id="other",
        chart_spec=None,
        rows=[
            ["202501", "DE", 100.0],
            ["202501", "FR", 200.0],
            ["202502", "DE", 150.0],
            ["202502", "FR", 250.0],
        ],
    )
    sources = {"result": source, "other": other}
    _, _, charts = composition_catalog(request, sources)
    selected = [
        next(key for key, widget in charts.items() if widget.result_id == identity)
        for identity in sources
    ]
    model = Designer(
        {
            "result_id": "result",
            "charts": [
                {"candidate_id": selected[0], "title": "Revenue 2025"},
                {"candidate_id": selected[1]},
            ],
        }
    )
    outcome = asyncio.run(
        design_dashboard(
            source=source,
            request=request,
            sources=sources,
            completer=model,
            provider="test",
            model="script",
            budget=RequestBudget(3),
        )
    )
    assert outcome.status == "FALLBACK"
    assert outcome.failures[0].code == "UNSUPPORTED_NUMERICAL_CLAIM"
    assert outcome.failures[0].field == "charts.0.title"


@pytest.mark.parametrize(
    "label,code",
    [
        ("Revenue 2099", "UNSUPPORTED_NUMERICAL_CLAIM"),
        ("Revenue increased 12%", "UNSUPPORTED_NUMERICAL_CLAIM"),
        ("Revenue $2026", "UNSUPPORTED_NUMERICAL_CLAIM"),
        ("**Revenue**", "ANNOTATION_MARKUP"),
        ("<b>Revenue</b>", "ANNOTATION_MARKUP"),
    ],
)
def test_unverified_numbers_and_markup_have_precise_safe_fields(label, code):
    outcome, *_ = run({"result_id": "result", "title": label})
    assert outcome.status == "FALLBACK"
    assert outcome.failures[0].code == code and outcome.failures[0].field == "title"
    assert outcome.failures[0].repairable


@pytest.mark.parametrize(
    "patch,code,path",
    [
        ({"result_id": "unrelated"}, "SOURCE_MISMATCH", "result_id"),
        ({"charts": [{"candidate_id": "invented"}]}, "INVALID_CANDIDATE", "charts.0.candidate_id"),
        ({"mode": "PATCH"}, "MODE_MISMATCH", "mode"),
        ({"rationale": "x" * 601}, "ANNOTATION_TOO_LONG", "rationale"),
    ],
)
def test_rejections_have_structured_internal_diagnostics(patch, code, path):
    outcome, model, budget = run({"result_id": "result", **patch})
    assert outcome.failures[0].code == code and outcome.failures[0].field == path
    assert outcome.failures[0].request_id and outcome.failures[0].proposal_id
    assert outcome.failures[0].attempt == 1 and model.calls == budget.model_calls == 1


def test_axes_stale_replacements_and_replace_mode_keep_previous_widgets():
    _, _, _, _, charts = setup()
    key = next(iter(charts))
    previous = run({"result_id": "result", "charts": [{"candidate_id": key}]})[0].spec
    for change, code in [
        ({"charts": [{"candidate_id": key, "y": "invented"}]}, "INVALID_AXIS"),
        ({"charts": [{"candidate_id": key, "replaces_widget_id": "stale"}]}, "STALE_WIDGET"),
        ({"mode": "REPLACE"}, "MODE_MISMATCH"),
    ]:
        outcome, *_ = run({"result_id": "result", "mode": "PATCH", **change}, previous=previous)
        assert outcome.status == "FALLBACK" and outcome.failures[0].code == code
        assert outcome.spec.widgets == previous.widgets


def test_one_specific_repair_preserves_valid_choices_and_never_logs_model_text(caplog):
    source, request, _, kpis, charts = setup()
    secret = "PRIVATE_MODEL_TEXT_" * 40
    candidate = {
        "result_id": "result",
        "rationale": secret,
        "charts": [{"candidate_id": next(iter(charts))}],
        "kpis": [{"kpi_id": next(iter(kpis))}],
    }

    class Correcting(Designer):
        async def complete_task(self, model, prompt, task, **kwargs):
            self.calls += 1
            if self.calls == 2:
                context = json.loads(
                    prompt.split(
                        "Verified context (profiles/sample are bounded, not the entire result): "
                    )[1]
                )
                feedback = json.loads(context["repair_feedback"])
                assert feedback["code"] == "ANNOTATION_TOO_LONG"
                assert feedback["field"] == "rationale"
                assert context["previous_proposal_for_repair"]["charts"] == candidate["charts"]
                assert context["previous_proposal_for_repair"]["kpis"] == candidate["kpis"]
            value = candidate if self.calls == 1 else {**candidate, "rationale": "Compare metrics."}
            return json.dumps(value), TokenUsage(input_tokens=10, output_tokens=20)

    model = Correcting({})
    caplog.set_level(logging.WARNING, logger="semantic_text2sql.ai_dashboard_designer")
    budget = RequestBudget(3, max_model_calls=2, max_database_calls=0)
    outcome = asyncio.run(
        design_dashboard(
            source=source,
            request=request,
            completer=model,
            provider="test",
            model="script",
            budget=budget,
            repair=True,
        )
    )
    assert outcome.status == "READY" and model.calls == 2 and budget.database_calls == 0
    assert outcome.usage.total_tokens == 60
    assert [item.status for item in outcome.ledger] == ["FAILED", "SUCCEEDED"]
    assert outcome.failures[0].field == "rationale"
    assert "request_id=" in caplog.text and "proposal_id=" in caplog.text
    assert "attempt=1" in caplog.text and "field=rationale" in caplog.text
    assert secret not in caplog.text and source.sql not in caplog.text


def test_two_failed_repairs_preserve_previous_and_unknown_fields_are_not_logged(caplog):
    source, request, _, _, charts = setup()
    key = next(iter(charts))
    previous = run({"result_id": "result", "charts": [{"candidate_id": key}]})[0].spec
    before = copy.deepcopy(previous.model_dump())
    model = Designer({"result_id": "result", "mode": "PATCH", "rationale": "secret" * 200})
    outcome = asyncio.run(
        design_dashboard(
            source=source,
            request=request,
            previous=previous,
            completer=model,
            provider="test",
            model="script",
            budget=RequestBudget(3),
            repair=True,
        )
    )
    assert outcome.status == "FALLBACK" and model.calls == 2
    assert [f.attempt for f in outcome.failures] == [1, 2]
    assert len({f.request_id for f in outcome.failures}) == 1
    assert len({f.proposal_id for f in outcome.failures}) == 2
    assert previous.model_dump() == before and outcome.spec.widgets == previous.widgets
    leaked = run({"result_id": "result", "PASSWORD_DO_NOT_LOG": "RAW_RESULT_SECRET"})[0]
    assert leaked.failures[0].field == "unknown_field"
    assert "PASSWORD_DO_NOT_LOG" not in caplog.text and "RAW_RESULT_SECRET" not in caplog.text


@pytest.mark.parametrize("status", [400, 429, 500])
def test_invalid_provider_requests_are_not_repaired(status):
    source, request, *_ = setup()

    class Rejected(Designer):
        async def complete_task(self, *args, **kwargs):
            self.calls += 1
            raise ProviderRequestError(
                "test", "script", "structured", status, "invalid_request", None, 1
            )

    model = Rejected({})
    outcome = asyncio.run(
        design_dashboard(
            source=source,
            request=request,
            completer=model,
            provider="test",
            model="script",
            budget=RequestBudget(3),
            repair=True,
        )
    )
    assert model.calls == 1 and not outcome.failures[0].repairable
    assert outcome.failures[0].code == (
        "PROVIDER_REQUEST_REJECTED" if status == 400 else "PROVIDER_FAILURE"
    )


def test_repair_cannot_change_other_valid_plot_selections():
    source, request, _, _, charts = setup()
    ids = list(charts)

    class Redesigning(Designer):
        async def complete_task(self, *args, **kwargs):
            self.calls += 1
            return json.dumps(
                {
                    "result_id": "result",
                    "rationale": "x" * 601 if self.calls == 1 else "Compare the accepted data.",
                    "charts": [{"candidate_id": ids[0] if self.calls == 1 else ids[1]}],
                }
            ), TokenUsage()

    model = Redesigning({})
    outcome = asyncio.run(
        design_dashboard(
            source=source,
            request=request,
            completer=model,
            provider="test",
            model="script",
            budget=RequestBudget(3),
            repair=True,
        )
    )
    assert outcome.status == "FALLBACK" and model.calls == 2
    assert [failure.code for failure in outcome.failures] == [
        "ANNOTATION_TOO_LONG",
        "REPAIR_SCOPE_VIOLATION",
    ]


def test_api_returns_useful_diagnostics_and_failed_edit_does_not_replace_revision(
    registry,
    monkeypatch,
    tmp_path,
):
    for role in ("ASSISTANT", "DASHBOARD"):
        monkeypatch.setenv(f"TEXT2SQL_{role}_PROVIDER", "agentrouter")
        monkeypatch.setenv(f"TEXT2SQL_{role}_MODEL", "script")
    monkeypatch.setenv("TEXT2SQL_CONVERSATIONAL_AGENT_ENABLED", "true")
    monkeypatch.setenv("TEXT2SQL_CONTEXT_MODE", "retrieval")
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    monkeypatch.setenv("TEXT2SQL_RUN_HISTORY_PATH", str(tmp_path / "history.sqlite"))

    class SQL:
        calls = 0

        async def generate(self, **kwargs):
            self.calls += 1
            return "SELECT status, SUM(amount) AS total FROM orders GROUP BY status", 1

    class Model:
        designs = 0

        async def complete_detailed(self, model, prompt):
            context = json.loads(prompt.split("\nContext: ")[1])
            if context["question"] == "Sum amount by status":
                decision = {
                    "action": "CALL_TOOL",
                    "intent": "NEW_QUERY",
                    "tool": "ask_data",
                    "tool_arguments": {"objective": context["question"], "operation": "NEW_QUERY"},
                    "finish_after_tool": True,
                }
            else:
                decision = {
                    "action": "CALL_TOOL",
                    "intent": "VISUALIZE",
                    "tool": "ai_dashboard_designer",
                    "tool_arguments": {"objective": context["question"]},
                }
            return json.dumps(decision), TokenUsage()

        async def complete_task(self, model, prompt, task, **kwargs):
            if task != "dashboard":
                return await self.complete_detailed(model, prompt)
            self.designs += 1
            context = json.loads(
                prompt.split(
                    "Verified context (profiles/sample are bounded, not the entire result): "
                )[1]
            )
            candidate = {"result_id": context["result_id"]}
            if self.designs == 1:
                candidate["charts"] = [
                    {
                        "candidate_id": context["chart_catalog"][0]["candidate_id"],
                        "analytical_role": "PRIMARY",
                    }
                ]
            else:
                candidate.update(mode="PATCH", rationale="x" * 601)
            return json.dumps(candidate), TokenUsage(input_tokens=5, output_tokens=5)

    sql, model = SQL(), Model()
    client = TestClient(
        create_app(TextToSQLAgent(registry, sql), conversation_completers={"agentrouter": model}),
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50000),
    )
    payload = {
        "session_id": "diagnostics",
        "db_id": "shop",
        "provider": "agentrouter",
        "model": "script",
    }
    assert client.post("/api/chat", json={**payload, "message": "Sum amount by status"}).json()[
        "generation"
    ]["accepted"]
    built = client.post("/api/chat", json={**payload, "message": "Design a dashboard"}).json()
    assert built["dashboard"]
    failed = client.post("/api/chat", json={**payload, "message": "Change the theme"}).json()
    assert failed["dashboard"] is None and "600" in failed["message"]
    assert len(failed["dashboard_design_failures"]) == 2
    assert failed["dashboard_design_failures"][-1]["field"] == "rationale"
    assert model.designs == 3 and sql.calls == 1
    saved = client.post(
        "/api/studio",
        json={
            "session_id": "diagnostics",
            "db_id": "shop",
            "result_id": built["result_id"],
            "design_revision_id": built["dashboard"]["revision_id"],
            "name": "Unchanged original",
        },
    )
    assert saved.status_code == 200, saved.text
