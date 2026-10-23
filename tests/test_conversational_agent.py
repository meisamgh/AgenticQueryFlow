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
from semantic_text2sql.models import ChatRequest, ChatResponse, ConversationState, TokenUsage
from semantic_text2sql.profiling import ProfileStore
from semantic_text2sql.run_history import RunHistoryStore


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


def test_agent_answers_greeting_without_model_or_sql(registry: Any, tmp_path: Any) -> None:
    calls: list[str] = []
    completer = Completer([])
    agent = _agent(registry, tmp_path, completer, calls)
    response = asyncio.run(
        agent.run(ChatRequest(session_id="hello", db_id="shop", message="Hello"))
    )
    assert "Hello" in response.message
    assert completer.calls == 0
    assert calls == []


def test_agent_can_inspect_then_answer_without_sql(registry: Any, tmp_path: Any) -> None:
    calls: list[str] = []
    completer = Completer(
        [
            {
                "action": "CALL_TOOL",
                "intent": "EXPLORE_SCHEMA",
                "tool": "list_tables",
                "instruction": "List tables",
                "answer": None,
            },
            {
                "action": "ANSWER",
                "intent": "EXPLORE_SCHEMA",
                "tool": None,
                "instruction": None,
                "answer": "The database contains a customers table.",
            },
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


def test_agent_delegates_analytical_question_once(registry: Any, tmp_path: Any) -> None:
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
    assert completer.calls == 1


def test_analytical_wording_is_not_mistaken_for_a_definition() -> None:
    assert _route("What does revenue look like over time?") == "sql"
    assert _route("What does retention mean?") == "business"
    assert _stored_grain_capability(["year_month", "revenue"]) > 0


def test_used_columns_are_read_from_accepted_sql(registry: Any, tmp_path: Any) -> None:
    calls: list[str] = []
    completer = Completer([])
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
    assert completer.calls == 0
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
    completer = Completer([])
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
    assert completer.calls == 0


def test_invalid_agent_decision_fails_closed(registry: Any, tmp_path: Any) -> None:
    calls: list[str] = []
    completer = Completer([{"action": "CALL_TOOL", "tool": "run_arbitrary_sql"}])
    agent = _agent(registry, tmp_path, completer, calls)
    response = asyncio.run(
        agent.run(ChatRequest(session_id="invalid", db_id="shop", message="Inspect this database"))
    )
    assert "could not complete this safely" in response.message
    assert calls == []
