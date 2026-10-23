from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi.testclient import TestClient

from semantic_text2sql.agent import TextToSQLAgent
from semantic_text2sql.api import create_app
from semantic_text2sql.conversational_agent import ConversationalAnalyticsAgent
from semantic_text2sql.conversational_bridge import ConversationalAnalyticsBridge, _route
from semantic_text2sql.data_requirement_validator import _stored_grain_capability
from semantic_text2sql.glossary import GlossaryStore
from semantic_text2sql.hybrid_retrieval import HybridSchemaRetriever
from semantic_text2sql.models import (
    CallLedgerEntry,
    ChatRequest,
    ChatResponse,
    ConversationState,
    GenerateResponse,
    StrategyHints,
    TokenUsage,
)
from semantic_text2sql.profiling import ProfileStore
from semantic_text2sql.result_cache import RecentResult
from semantic_text2sql.run_history import RunHistoryStore
from semantic_text2sql.runtime import RequestBudget, active_request_budget, use_request_budget


class Encoder:
    def encode(self, texts: Any) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


class Completer:
    def __init__(self, decisions: list[dict[str, Any]]) -> None:
        self.decisions = iter(decisions)
        self.calls = 0

    async def complete_detailed(self, model: str, prompt: str) -> tuple[str, TokenUsage]:
        self.calls += 1
        return json.dumps(next(self.decisions)), TokenUsage(input_tokens=10, output_tokens=5)


def _agent(
    registry: Any, tmp_path: Any, completer: Completer, calls: list[str]
) -> ConversationalAnalyticsAgent:
    async def sql_handler(request: ChatRequest) -> ChatResponse:
        calls.append(request.message)
        return ChatResponse(
            session_id=request.session_id,
            operation="NEW_QUERY",
            resolved_question=request.message,
            message="The query ran.",
        )

    bridge = ConversationalAnalyticsBridge(
        database=registry,
        postgres=None,
        profiles=ProfileStore(tmp_path / "profiles"),
        glossaries=GlossaryStore(tmp_path / "glossaries"),
        retriever=HybridSchemaRetriever(Encoder()),
        sql_handler=sql_handler,
    )
    return ConversationalAnalyticsAgent(
        bridge=bridge,
        completers={"agentrouter": completer},
        get_state=lambda session_id: None,
        get_last_response=lambda session_id, db_id: None,
        sql_handler=sql_handler,
    )


def test_agent_answers_greeting_with_one_model_call_and_no_sql(
    registry: Any, tmp_path: Any
) -> None:
    calls: list[str] = []
    completer = Completer([{
        "action": "ANSWER", "intent": "DIRECT_ANSWER", "tool": None,
        "instruction": None, "answer": "Hello! Ask me about your data.",
    }])
    agent = _agent(registry, tmp_path, completer, calls)
    response = asyncio.run(
        agent.run(ChatRequest(session_id="hello", db_id="shop", message="Hello"))
    )
    assert "Hello" in response.message
    assert completer.calls == 1
    assert calls == []


def test_agent_can_inspect_then_answer_without_sql(registry: Any, tmp_path: Any) -> None:
    calls: list[str] = []
    completer = Completer(
        [
            {"action": "CALL_TOOL", "intent": "EXPLORE_SCHEMA", "tool": "list_tables",
             "instruction": "List available tables", "answer": None},
            {"action": "ANSWER", "intent": "EXPLORE_SCHEMA", "tool": None,
             "instruction": None, "answer": "The database contains a customers table."},
        ]
    )
    agent = _agent(registry, tmp_path, completer, calls)
    response = asyncio.run(
        agent.run(
            ChatRequest(session_id="schema", db_id="shop", message="Which tables are available?")
        )
    )
    assert response.message == "The database contains a customers table."
    assert completer.calls == 2
    assert calls == []


def test_agent_delegates_analytical_question_and_explains_it(registry: Any, tmp_path: Any) -> None:
    calls: list[str] = []
    completer = Completer(
        [
            {
                "action": "CALL_TOOL",
                "intent": "NEW_QUERY",
                "tool": "ask_data",
                "instruction": "Count customers",
                "answer": None,
            },
            {
                "action": "ANSWER",
                "intent": "NEW_QUERY",
                "tool": None,
                "instruction": None,
                "answer": "The query ran.",
            },
        ]
    )
    agent = _agent(registry, tmp_path, completer, calls)
    response = asyncio.run(
        agent.run(
            ChatRequest(session_id="sql", db_id="shop", message="How many customers are there?")
        )
    )
    assert response.message == "The query ran."
    assert calls == ["How many customers are there?"]
    assert completer.calls == 2


def test_final_model_failure_preserves_governed_result(
    registry: Any, tmp_path: Any
) -> None:
    calls: list[str] = []
    completer = Completer([
        {"action": "CALL_TOOL", "intent": "NEW_QUERY", "tool": "ask_data",
         "instruction": "Count customers", "answer": None},
    ])
    agent = _agent(registry, tmp_path, completer, calls)
    result = asyncio.run(agent.run(ChatRequest(
        session_id="fallback", db_id="shop", message="Count customers"
    )))
    assert calls == ["Count customers"]
    assert result.operation == "NEW_QUERY"
    assert result.message == "The query ran."
    assert "assistant_response_unavailable" in result.provenance
    assert result.token_usage.total_tokens == 15
    assert any(item.status == "FAILED" for item in result.call_ledger)


def test_analytical_wording_is_not_mistaken_for_a_definition() -> None:
    assert _route("What does revenue look like over time?") == "sql"
    assert _route("What does retention mean?") == "business"
    assert _stored_grain_capability(["year_month", "revenue"]) > 0


def test_used_columns_are_read_from_accepted_sql(registry: Any, tmp_path: Any) -> None:
    calls: list[str] = []
    completer = Completer([
        {"action": "CALL_TOOL", "intent": "EXPLAIN", "tool": "explain_existing_result",
         "instruction": "Inspect columns referenced by the accepted SQL", "answer": None},
        {"action": "ANSWER", "intent": "EXPLAIN", "tool": None,
         "instruction": None, "answer": "The accepted SQL used c.country and c.customer_id."},
    ])
    agent = _agent(registry, tmp_path, completer, calls)
    previous = ConversationState(
        session_id="prior",
        db_id="shop",
        root_question="Count customers",
        resolved_question="Count customers",
        last_sql="SELECT c.country, COUNT(c.customer_id) FROM customers AS c GROUP BY c.country",
    )
    agent.get_state = lambda session_id: previous
    response = asyncio.run(
        agent.run(
            ChatRequest(session_id="prior", db_id="shop", message="Which columns did you use?")
        )
    )
    assert "c.country" in response.message
    assert "c.customer_id" in response.message
    assert completer.calls == 2
    assert calls == []


def test_opt_in_agent_uses_existing_api_contract(
    registry: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    class SQLModel:
        async def generate(self, **kwargs: Any) -> tuple[str, int]:
            return "SELECT COUNT(*) FROM customers", 1

    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    monkeypatch.setenv("TEXT2SQL_PROFILE_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setenv("TEXT2SQL_GLOSSARY_ROOT", str(tmp_path / "glossaries"))
    monkeypatch.setenv("TEXT2SQL_CONVERSATIONAL_AGENT_ENABLED", "true")
    completer = Completer([{
        "action": "ANSWER", "intent": "DIRECT_ANSWER", "tool": None,
        "instruction": None, "answer": "Hello!",
    }])
    client = TestClient(
        create_app(
            TextToSQLAgent(registry, SQLModel()),
            conversation_completers={"agentrouter": completer},
            run_history=RunHistoryStore(None),
        )
    )
    response = client.post(
        "/api/chat",
        json={
            "session_id": "agent-api",
            "db_id": "shop",
            "model": "test-model",
            "message": "Hello",
        },
    )
    assert response.status_code == 200
    assert response.json()["operation"] == "EXPLAIN_CONTEXT"
    assert completer.calls == 1


def test_invalid_agent_decision_fails_closed(registry: Any, tmp_path: Any) -> None:
    calls: list[str] = []
    completer = Completer([{"action": "CALL_TOOL", "tool": "run_arbitrary_sql"}])
    agent = _agent(registry, tmp_path, completer, calls)
    response = asyncio.run(
        agent.run(ChatRequest(session_id="invalid", db_id="shop", message="Inspect this database"))
    )
    assert "could not complete this safely" in response.message
    assert calls == []


def test_assistant_failure_is_marked_not_presented_as_model_answer(
    registry: Any, tmp_path: Any
) -> None:
    agent = _agent(registry, tmp_path, Completer([]), [])
    result = asyncio.run(
        agent.run(ChatRequest(session_id="down", db_id="shop", message="Hello"))
    )
    assert "Assistant response unavailable" in result.message
    assert result.provenance == ["assistant_service_failure"]


def _accepted_result() -> RecentResult:
    response = ChatResponse(
        session_id="analysis",
        operation="NEW_QUERY",
        resolved_question="Show monthly revenue",
        message="The query ran.",
        generation=GenerateResponse(
            db_id="shop",
            question="Show monthly revenue",
            provider="agentrouter",
            model="test-model",
            strategy=StrategyHints(mode="exact"),
            sql="SELECT month, revenue FROM revenue_by_month",
            accepted=True,
            termination_reason="accepted",
            columns=["month", "revenue"],
            rows=[["2025-01", 10], ["2025-02", 15]],
            row_count=2,
        ),
    )
    return RecentResult.from_response(response)


def test_analyze_result_calculates_verified_change() -> None:
    analysis = ConversationalAnalyticsAgent._analyze_result(_accepted_result())
    assert analysis["columns"]["revenue"]["mean"] == 12.5
    assert analysis["observed_trend"]["absolute_change"] == 5
    assert analysis["observed_trend"]["percent_change"] == 50
    assert analysis["observed_trend"]["decline_observed"] is False
    assert analysis["rankings"]["revenue"]["highest_rows"] == [("2025-02", 15.0)]


def test_metadata_capabilities_are_distinct(registry: Any, tmp_path: Any) -> None:
    agent = _agent(registry, tmp_path, Completer([]), [])
    tables = asyncio.run(agent.bridge.inspect_metadata("shop", "list_tables"))
    detail = asyncio.run(agent.bridge.inspect_metadata("shop", "describe_table", "customers"))
    relationships = asyncio.run(
        agent.bridge.inspect_metadata("shop", "get_relationships", "orders")
    )
    assert any("customers:" in fact for fact in tables.facts)
    assert any("customer_id" in fact for fact in detail.facts)
    assert all("→" in fact for fact in relationships.facts) or not relationships.found
    assert tables.evidence_id != detail.evidence_id


def test_false_decline_premise_can_stop_without_sql(registry: Any, tmp_path: Any) -> None:
    calls: list[str] = []
    completer = Completer(
        [
            {"action": "CALL_TOOL", "intent": "INVESTIGATE", "tool": "analyze_result",
             "instruction": "Check whether revenue declined", "answer": None},
            {"action": "ANSWER", "intent": "INVESTIGATE", "tool": None,
             "instruction": None, "answer": "Revenue rose from 10 to 15; no decline is shown."},
        ]
    )
    agent = _agent(registry, tmp_path, completer, calls)
    agent.get_last_response = lambda session_id, db_id: _accepted_result()
    result = asyncio.run(
        agent.run(
            ChatRequest(session_id="analysis", db_id="shop", message="Why did revenue decline?")
        )
    )
    assert "no decline" in result.message
    assert calls == []
    assert completer.calls == 2


def test_agent_investigation_uses_distinct_objectives_and_preserves_session(
    registry: Any, tmp_path: Any
) -> None:
    calls: list[tuple[str, str]] = []

    async def sql_handler(request: ChatRequest) -> ChatResponse:
        calls.append((request.session_id, request.message))
        return ChatResponse(
            session_id=request.session_id,
            operation="NEW_QUERY",
            resolved_question=request.message,
            message="The query ran.",
            token_usage=TokenUsage(input_tokens=7, output_tokens=3),
            call_ledger=[
                CallLedgerEntry(
                    component="sql_generation", kind="MODEL", status="SUCCEEDED",
                    input_tokens=7, output_tokens=3,
                )
            ],
        )

    completer = Completer(
        [
            {"action": "CALL_TOOL", "intent": "INVESTIGATE", "tool": "ask_data",
             "instruction": "Revenue by month in 2025", "answer": None},
            {"action": "CALL_TOOL", "intent": "INVESTIGATE", "tool": "ask_data",
             "instruction": "Revenue by country in September 2025", "answer": None},
            {"action": "ANSWER", "intent": "INVESTIGATE", "tool": None,
             "instruction": None, "answer": "The available breakdowns do not establish a cause."},
        ]
    )
    agent = _agent(registry, tmp_path, completer, [])
    agent.sql_handler = sql_handler
    result = asyncio.run(
        agent.run(ChatRequest(session_id="analysis", db_id="shop", message="Why did revenue fall?"))
    )
    assert [message for _, message in calls] == [
        "Revenue by month in 2025", "Revenue by country in September 2025"
    ]
    assert len({session for session, _ in calls}) == 2
    assert result.session_id == "analysis"
    assert completer.calls == 3
    assert result.token_usage.total_tokens == 65
    assert sum(item.component == "sql_generation" for item in result.call_ledger) == 2


def test_repeated_governed_tools_count_each_model_call_once(
    registry: Any, tmp_path: Any
) -> None:
    calls: list[str] = []

    async def sql_handler(request: ChatRequest) -> ChatResponse:
        calls.append(request.message)
        return ChatResponse(
            session_id=request.session_id,
            operation="NEW_QUERY",
            resolved_question=request.message,
            message="Executed.",
            token_usage=TokenUsage(input_tokens=7, output_tokens=3),
            call_ledger=[CallLedgerEntry(
                component="sql_generation", kind="MODEL", status="SUCCEEDED",
                input_tokens=7, output_tokens=3,
            )],
        )

    completer = Completer([
        {"action": "CALL_TOOL", "intent": "NEW_QUERY", "tool": "ask_data",
         "instruction": "First query", "answer": None},
        {"action": "CALL_TOOL", "intent": "NEW_QUERY", "tool": "ask_data",
         "instruction": "Second query", "answer": None},
        {"action": "ANSWER", "intent": "NEW_QUERY", "tool": None,
         "instruction": None, "answer": "The second result is available."},
    ])
    agent = _agent(registry, tmp_path, completer, calls)
    agent.sql_handler = sql_handler
    result = asyncio.run(agent.run(
        ChatRequest(session_id="repeated", db_id="shop", message="Compare the data")
    ))
    assert len(calls) == 2
    assert result.token_usage.total_tokens == 65
    assert sum(item.component == "sql_generation" for item in result.call_ledger) == 2


def test_present_existing_result_does_not_call_sql(
    registry: Any, tmp_path: Any
) -> None:
    calls: list[str] = []
    completer = Completer([
        {"action": "CALL_TOOL", "intent": "EXPLAIN", "tool": "present_result",
         "instruction": "Show the accepted result", "answer": None},
        {"action": "ANSWER", "intent": "EXPLAIN", "tool": None,
         "instruction": None, "answer": "The stored result has two monthly rows."},
    ])
    agent = _agent(registry, tmp_path, completer, calls)
    agent.get_last_response = lambda session_id, db_id: _accepted_result()
    result = asyncio.run(agent.run(
        ChatRequest(session_id="analysis", db_id="shop", message="Show that result")
    ))
    assert result.message == "The stored result has two monthly rows."
    assert result.operation == "EXPLAIN_RESULT"
    assert result.generation is not None
    assert result.generation.rows == [["2025-01", 10], ["2025-02", 15]]
    assert calls == []


def test_correction_and_reset_use_the_existing_conversation_service(
    registry: Any, tmp_path: Any
) -> None:
    calls: list[str] = []
    completer = Completer([
        {"action": "ANSWER", "intent": "CORRECT", "tool": None,
         "instruction": None, "answer": "Corrected."},
        {"action": "ANSWER", "intent": "CORRECT", "tool": None,
         "instruction": None, "answer": "The correction was processed."},
        {"action": "ANSWER", "intent": "RESET_CONTEXT", "tool": None,
         "instruction": None, "answer": "Reset."},
        {"action": "ANSWER", "intent": "RESET_CONTEXT", "tool": None,
         "instruction": None, "answer": "The conversation was reset."},
    ])
    agent = _agent(registry, tmp_path, completer, calls)
    correction = asyncio.run(agent.run(ChatRequest(
        session_id="routing", db_id="shop", message="No, only paying customers"
    )))
    reset = asyncio.run(agent.run(ChatRequest(
        session_id="routing", db_id="shop", message="Start over"
    )))
    assert calls == ["No, only paying customers", "Start over"]
    assert correction.message == "The correction was processed."
    assert reset.message == "The conversation was reset."


def test_shared_budget_context_is_restored() -> None:
    first = RequestBudget(10, max_model_calls=1)
    assert active_request_budget() is None
    with use_request_budget(first):
        assert active_request_budget() is first
    assert active_request_budget() is None


def test_delegated_tool_cannot_bypass_parent_model_budget(
    registry: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("TEXT2SQL_ASSISTANT_MAX_MODEL_CALLS", "1")
    calls: list[str] = []

    async def governed_handler(request: ChatRequest) -> ChatResponse:
        calls.append(request.message)
        budget = active_request_budget()
        assert budget is not None
        budget.claim("model")
        return ChatResponse(
            session_id=request.session_id, operation="NEW_QUERY",
            resolved_question=request.message, message="Unexpected execution",
        )

    completer = Completer(
        [{"action": "CALL_TOOL", "intent": "NEW_QUERY", "tool": "ask_data",
          "instruction": "Count customers", "answer": None}]
    )
    agent = _agent(registry, tmp_path, completer, [])
    agent.sql_handler = governed_handler
    response = asyncio.run(
        agent.run(ChatRequest(session_id="budget", db_id="shop", message="Count customers"))
    )
    assert calls == ["Count customers"]
    assert "budget" in response.message.casefold()


def test_plot_followup_is_selected_by_top_model_and_uses_no_new_sql(
    registry: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    class SQLModel:
        calls = 0

        async def generate(self, **kwargs: Any) -> tuple[str, int]:
            self.calls += 1
            return "SELECT country, COUNT(*) AS total FROM customers GROUP BY country", 1

    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    monkeypatch.setenv("TEXT2SQL_PROFILE_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setenv("TEXT2SQL_GLOSSARY_ROOT", str(tmp_path / "glossaries"))
    monkeypatch.setenv("TEXT2SQL_CONVERSATIONAL_AGENT_ENABLED", "true")
    model = SQLModel()
    completer = Completer(
        [
            {"action": "CALL_TOOL", "intent": "NEW_QUERY", "tool": "ask_data",
             "instruction": "Count customers by country", "answer": None},
            {"action": "ANSWER", "intent": "NEW_QUERY", "tool": None,
             "instruction": None, "answer": "Here are customer counts by country."},
            {"action": "CALL_TOOL", "intent": "VISUALIZE", "tool": "change_visualization",
             "instruction": "Plot the accepted customer counts", "answer": None},
            {"action": "ANSWER", "intent": "VISUALIZE", "tool": None,
             "instruction": None, "answer": "Here is a chart of those customer counts."},
            {"action": "CALL_TOOL", "intent": "VISUALIZE", "tool": "change_visualization",
             "instruction": "Plot the accepted customer counts", "answer": None},
            {"action": "ANSWER", "intent": "VISUALIZE", "tool": None,
             "instruction": None, "answer": "The stored customer counts remain charted."},
            {"action": "CALL_TOOL", "intent": "RESET_CONTEXT", "tool": "reset_context",
             "instruction": "Reset this conversation", "answer": None},
            {"action": "ANSWER", "intent": "RESET_CONTEXT", "tool": None,
             "instruction": None, "answer": "The conversation was reset."},
        ]
    )
    history = RunHistoryStore(None)
    client = TestClient(
        create_app(
            TextToSQLAgent(registry, model),
            conversation_completers={"agentrouter": completer},
            run_history=history,
        )
    )
    base = {
        "session_id": "agent-chart", "db_id": "shop", "provider": "agentrouter",
        "model": "test-model",
    }
    initial = client.post(
        "/api/chat", json={**base, "message": "Count customers by country."}
    )
    assert initial.status_code == 200
    assert initial.json()["generation"]["accepted"] is True
    saved_initial = history.get(initial.json()["run_id"], "agent-chart")
    assert saved_initial is not None
    assert saved_initial.response_payload["message"] == "Here are customer counts by country."
    assert saved_initial.token_usage["input_tokens"] == (
        initial.json()["token_usage"]["input_tokens"]
    )
    assert saved_initial.token_usage["output_tokens"] == (
        initial.json()["token_usage"]["output_tokens"]
    )
    assert model.calls == 1
    assert completer.calls == 2
    plotted = client.post("/api/chat", json={**base, "message": "Plot it."})
    assert plotted.status_code == 200
    assert plotted.json()["presentation"]["chart_spec"] is not None
    assert plotted.json()["message"] == "Here is a chart of those customer counts."
    assert any(
        item["component"] == "result_cache_reuse" for item in plotted.json()["call_ledger"]
    )
    assert model.calls == 1
    assert completer.calls == 4
    saved_plot = history.get(plotted.json()["run_id"], "agent-chart")
    assert saved_plot is not None
    assert saved_plot.response_payload["message"] == plotted.json()["message"]
    bare_plot = client.post("/api/chat", json={**base, "message": "Plot"})
    assert bare_plot.status_code == 200
    assert bare_plot.json()["presentation"]["chart_spec"] is not None
    assert model.calls == 1
    assert completer.calls == 6
    reset = client.post("/api/chat", json={**base, "message": "Start over"})
    assert reset.status_code == 200
    assert reset.json()["operation"] == "RESET_CONTEXT"
    assert reset.json()["message"] == "The conversation was reset."
    assert model.calls == 1
    assert completer.calls == 8


def test_investigation_uses_governed_sql_for_distinct_breakdowns(
    registry: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    class BreakdownSQL:
        def __init__(self) -> None:
            self.questions: list[str] = []

        async def generate(self, **kwargs: Any) -> tuple[str, int]:
            question = str(kwargs.get("question", ""))
            self.questions.append(question)
            if "country" in question.casefold():
                return "SELECT country, COUNT(*) AS n FROM customers GROUP BY country", 1
            return "SELECT COUNT(*) AS n FROM orders", 1

    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    monkeypatch.setenv("TEXT2SQL_PROFILE_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setenv("TEXT2SQL_GLOSSARY_ROOT", str(tmp_path / "glossaries"))
    monkeypatch.setenv("TEXT2SQL_CONVERSATIONAL_AGENT_ENABLED", "true")
    model = BreakdownSQL()
    completer = Completer(
        [
            {"action": "CALL_TOOL", "intent": "INVESTIGATE", "tool": "ask_data",
             "instruction": "Count orders", "answer": None},
            {"action": "CALL_TOOL", "intent": "INVESTIGATE", "tool": "ask_data",
             "instruction": "Count customers by country", "answer": None},
            {"action": "ANSWER", "intent": "INVESTIGATE", "tool": None,
             "instruction": None,
             "answer": "These breakdowns are descriptive and do not establish a cause."},
        ]
    )
    client = TestClient(
        create_app(
            TextToSQLAgent(registry, model),
            conversation_completers={"agentrouter": completer},
            run_history=RunHistoryStore(None),
        )
    )
    response = client.post(
        "/api/chat",
        json={"session_id": "investigate", "db_id": "shop", "provider": "agentrouter",
              "model": "test-model", "message": "Why did orders change?"},
    )
    assert response.status_code == 200
    assert response.json()["message"].startswith("These breakdowns")
    assert model.questions == ["Count orders", "Count customers by country"]
    assert completer.calls == 3
