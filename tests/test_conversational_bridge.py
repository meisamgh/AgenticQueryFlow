from __future__ import annotations

import asyncio
from typing import Any

from fastapi.testclient import TestClient

from semantic_text2sql.agent import TextToSQLAgent
from semantic_text2sql.api import create_app
from semantic_text2sql.conversational_bridge import ConversationalAnalyticsBridge
from semantic_text2sql.glossary import BusinessGlossary, GlossaryStore, GlossaryTerm
from semantic_text2sql.hybrid_retrieval import HybridSchemaRetriever
from semantic_text2sql.models import ChatRequest, ChatResponse
from semantic_text2sql.profiling import ProfileStore
from semantic_text2sql.run_history import RunHistoryStore


class SmallEncoder:
    def encode(self, texts: Any) -> list[list[float]]:
        return [[1.0, float("customer" in text.casefold())] for text in texts]


class FixedSQLModel:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, **kwargs: Any) -> tuple[str, int]:
        self.calls += 1
        return "SELECT COUNT(*) AS customer_count FROM customers", 1


def test_bridge_routes_metadata_without_sql_and_preserves_sql_handler(
    registry: Any, tmp_path: Any
) -> None:
    glossary_root = tmp_path / "glossaries"
    glossary_root.mkdir()
    (glossary_root / "shop.json").write_text(
        BusinessGlossary(
            db_id="shop",
            version="1",
            source="approved",
            precedence=["glossary"],
            terms=[GlossaryTerm(term="retention", definition="Customers returning in the period.")],
        ).model_dump_json()
    )
    calls: list[str] = []

    async def sql_handler(request: ChatRequest) -> ChatResponse:
        calls.append(request.message)
        return ChatResponse(
            session_id=request.session_id,
            operation="NEW_QUERY",
            resolved_question=request.message,
            message="SQL route",
        )

    bridge = ConversationalAnalyticsBridge(
        database=registry,
        postgres=None,
        profiles=ProfileStore(tmp_path / "profiles"),
        glossaries=GlossaryStore(glossary_root),
        retriever=HybridSchemaRetriever(SmallEncoder()),
        sql_handler=sql_handler,
    )
    base = {"session_id": "bridge-test", "db_id": "shop"}
    schema = asyncio.run(bridge.run(ChatRequest(**base, message="What tables are available?")))
    assert schema.route == "schema"
    assert "customers" in schema.response.message
    assert schema.response.provenance[0].startswith("search_schema-")
    assert calls == []

    business = asyncio.run(bridge.run(ChatRequest(**base, message="What does retention mean?")))
    assert business.route == "business"
    assert "Customers returning in the period" in business.response.message
    assert calls == []

    unknown = asyncio.run(bridge.run(ChatRequest(**base, message="Define churn")))
    assert "No approved business definition" in unknown.response.message
    analytical = asyncio.run(bridge.run(ChatRequest(**base, message="Count customers")))
    assert analytical.route == "sql"
    assert calls == ["Count customers"]


def test_opt_in_bridge_preserves_existing_chat_sql_path(
    registry: Any, monkeypatch: Any, tmp_path: Any
) -> None:
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    monkeypatch.setenv("TEXT2SQL_PROFILE_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setenv("TEXT2SQL_GLOSSARY_ROOT", str(tmp_path / "glossaries"))
    monkeypatch.setenv("TEXT2SQL_CONVERSATIONAL_BRIDGE_ENABLED", "true")
    model = FixedSQLModel()
    client = TestClient(
        create_app(TextToSQLAgent(registry, model), run_history=RunHistoryStore(None))
    )
    base = {"session_id": "bridge-api", "db_id": "shop", "model": "test-model"}
    schema = client.post("/api/chat", json={**base, "message": "What tables are available?"})
    assert schema.status_code == 200
    assert schema.json()["operation"] == "EXPLAIN_CONTEXT"
    assert model.calls == 0
    analytical = client.post("/api/chat", json={**base, "message": "Count customers"})
    assert analytical.status_code == 200
    assert analytical.json()["generation"]["accepted"] is True
    assert model.calls == 1
