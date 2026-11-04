from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from test_dashboard import snapshot

from semantic_text2sql.agent import TextToSQLAgent
from semantic_text2sql.ai_dashboard_designer import design_dashboard, eligible_catalog
from semantic_text2sql.dashboard_service import DashboardRequest
from semantic_text2sql.llm import OllamaCompleter, RoutingSQLModel
from semantic_text2sql.models import GenerateRequest
from semantic_text2sql.provider_registry import configured_model_options, supports_model
from semantic_text2sql.runtime import RequestBudget


def test_only_explicitly_configured_local_models_are_authorized(monkeypatch):
    monkeypatch.setenv("TEXT2SQL_SQL_PROVIDER", "ollama")
    monkeypatch.setenv("TEXT2SQL_SQL_MODEL", "qwen3.5:4b")
    assert supports_model("ollama", "qwen3.5:4b")
    assert not supports_model("ollama", "arbitrary-unconfigured-model")
    option = next(
        item
        for item in configured_model_options()
        if item.provider == "ollama" and item.model == "qwen3.5:4b"
    )
    assert option.local and option.configured


@pytest.mark.parametrize("safe", [True, False])
def test_local_sql_uses_existing_prompt_validation_and_read_only_execution(
    registry, monkeypatch, safe
):
    monkeypatch.setenv("TEXT2SQL_SQL_MAX_TOKENS", "456")
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload["model"] == "qwen3.5:4b" and "format" not in payload
        assert payload["options"]["num_predict"] == 456
        assert "customers" in payload["messages"][0]["content"]
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": "SELECT name FROM customers ORDER BY name"
                    if safe
                    else "DELETE FROM customers"
                },
                "prompt_eval_count": 30,
                "eval_count": 10,
            },
        )

    local = OllamaCompleter(transport=httpx.MockTransport(handler))
    router = RoutingSQLModel(local, local, local, local, ollama=local)
    if not safe:

        def forbidden(*args, **kwargs):
            pytest.fail("Unsafe local SQL must never execute")

        monkeypatch.setattr(registry, "execute", forbidden)
    result = asyncio.run(
        TextToSQLAgent(registry, router).generate(
            GenerateRequest(
                db_id="shop",
                question="List customer names",
                provider="ollama",
                model="qwen3.5:4b",
                execute=True,
            )
        )
    )
    assert calls and result.accepted == safe
    if safe:
        assert result.rows == [["Anna"], ["Luca"]]
    else:
        assert all(not attempt.validation.valid for attempt in result.attempts)


@pytest.mark.parametrize("grammar_rejection", [False, True])
def test_local_dashboard_schema_is_native_with_one_bounded_fallback(grammar_rejection):
    source = snapshot()
    request = DashboardRequest(session_id="owner", db_id="sales", result_id="result")
    _, _, charts = eligible_catalog(source, request)
    calls = []

    def handler(http_request):
        payload = json.loads(http_request.content)
        calls.append(payload)
        if len(calls) == 1:
            assert isinstance(payload["format"], dict)
            assert '"$defs":' not in payload["messages"][0]["content"]
            if grammar_rejection:
                return httpx.Response(400, json={"error": "failed to parse grammar"})
        else:
            assert grammar_rejection and payload["format"] == "json"
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": json.dumps(
                        {
                            "result_id": "result",
                            "charts": [{"candidate_id": next(iter(charts))}],
                        }
                    )
                },
                "prompt_eval_count": 30,
                "eval_count": 10,
            },
        )

    budget = RequestBudget(10)
    outcome = asyncio.run(
        design_dashboard(
            source=source,
            request=request,
            completer=OllamaCompleter(transport=httpx.MockTransport(handler)),
            provider="ollama",
            model="qwen3.5:4b",
            budget=budget,
            repair=True,
        )
    )
    assert outcome.status == "READY" and outcome.spec.widgets
    assert len(calls) == budget.model_calls == (2 if grammar_rejection else 1)
    assert outcome.ledger[-1].status == "SUCCEEDED"
