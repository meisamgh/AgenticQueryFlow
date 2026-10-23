from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from semantic_text2sql.agent import TextToSQLAgent
from semantic_text2sql.api import create_app
from semantic_text2sql.glossary import BusinessGlossary, GlossaryTerm
from semantic_text2sql.hybrid_retrieval import (
    FastEmbedEncoder,
    HybridSchemaRetriever,
    _bm25,
    _column_document,
    _dense_scores,
    _ranks,
    _relevant_glossary_terms,
    _rrf,
    _value_scores,
)
from semantic_text2sql.models import ChatResponse, GenerateResponse, StrategyHints
from semantic_text2sql.result_cache import RecentResult
from semantic_text2sql.run_history import RunHistoryStore


class StableEncoder:
    def encode(self, texts: Any) -> list[list[float]]:
        return [[float(len(text)), 1.0] for text in texts]


def test_fastembed_initializes_once_with_concurrent_callers(monkeypatch: Any) -> None:
    created: list[str] = []

    class FakeEmbedding:
        def __init__(self, model_name: str) -> None:
            created.append(model_name)

        def embed(self, texts: Any) -> list[list[float]]:
            return [[1.0, 2.0] for _ in texts]

    monkeypatch.setitem(sys.modules, "fastembed", SimpleNamespace(TextEmbedding=FakeEmbedding))
    encoder = FastEmbedEncoder("fake")
    with ThreadPoolExecutor(max_workers=8) as pool:
        vectors = list(pool.map(lambda _: encoder.encode(["same text"]), range(16)))
    assert created == ["fake"]
    assert vectors == [[[1.0, 2.0]]] * 16


def test_dense_initialization_failure_falls_back_to_bm25(registry: Any, monkeypatch: Any) -> None:
    class BrokenEmbedding:
        def __init__(self, model_name: str) -> None:
            raise RuntimeError("model unavailable")

    monkeypatch.setitem(sys.modules, "fastembed", SimpleNamespace(TextEmbedding=BrokenEmbedding))
    retriever = HybridSchemaRetriever(FastEmbedEncoder("broken"))
    reduced, selection, trace = retriever.retrieve(
        "customers country", None, registry.inspect("shop"), None, None
    )
    assert selection.tables
    assert reduced.tables
    assert trace.embedding_ranks == {}


def test_query_specific_glossary_does_not_pollute_cached_schema_documents(registry: Any) -> None:
    schema = registry.inspect("shop")
    glossary = BusinessGlossary(
        db_id="shop",
        version="1",
        source="test",
        precedence=["question", "glossary"],
        terms=[
            GlossaryTerm(term="spend", definition="Order amount", columns=["orders.amount"]),
            GlossaryTerm(
                term="location", definition="Customer country", columns=["customers.country"]
            ),
        ],
    )
    reused = HybridSchemaRetriever(StableEncoder())
    reused.retrieve("spend", None, schema, None, glossary)
    _, warm_selection, warm_trace = reused.retrieve("location", None, schema, None, glossary)
    _, fresh_selection, fresh_trace = HybridSchemaRetriever(StableEncoder()).retrieve(
        "location", None, schema, None, glossary
    )
    assert warm_selection == fresh_selection
    assert warm_trace.bm25_ranks == fresh_trace.bm25_ranks
    assert warm_trace.rrf_scores == fresh_trace.rrf_scores
    assert len(reused._document_cache) == 1
    # Reference ranking follows the original uncached per-question document
    # construction; compare all fused rank inputs, not just selected tables.
    query = "location"
    terms = _relevant_glossary_terms(query, glossary)
    documents: dict[str, str] = {}
    for table in schema.tables:
        column_texts = []
        for column in table.columns:
            document = _column_document(table.name, column, None, terms)
            documents[f"column:{table.name}.{column.name}"] = document
            column_texts.append(document)
        documents[f"table:{table.name}"] = " ".join([table.name, " ".join(column_texts)])
    bm25 = _ranks(_bm25(query, documents), positive_only=True)
    dense = _ranks(_dense_scores(query, documents, StableEncoder()), positive_only=False)
    values = _ranks(_value_scores(query, schema, {}), positive_only=True)
    fused = _rrf((bm25, dense, values), 60)
    assert warm_trace.bm25_ranks == bm25
    assert warm_trace.embedding_ranks == dense
    assert warm_trace.rrf_scores == {key: round(value, 8) for key, value in fused.items()}


def test_recent_result_retains_only_chart_reuse_fields() -> None:
    generation = GenerateResponse(
        db_id="shop",
        question="Show orders",
        provider="agentrouter",
        model="test",
        strategy=StrategyHints(mode="exact"),
        sql="SELECT id FROM orders",
        accepted=True,
        termination_reason="accepted",
        columns=["id"],
        rows=[[1]],
        row_count=1,
        model_context={
            "tables": {"orders": {"columns": ["id"]}},
            "large_unused_context": "x" * 10_000,
        },
    )
    response = ChatResponse(
        session_id="one",
        operation="NEW_QUERY",
        resolved_question="Show orders",
        message="One order",
        generation=generation,
        provenance=["run-1"],
    )
    cached = RecentResult.from_response(response)
    assert cached.sql == generation.sql
    assert cached.rows == generation.rows
    assert cached.provenance == ["run-1"]
    assert cached.generation.model_context == {"tables": {"orders": {"columns": ["id"]}}}
    assert not hasattr(cached, "conversation_interpretation")


def test_gzip_is_configurable_without_changing_json_contract(
    registry: Any, monkeypatch: Any, tmp_path: Any
) -> None:
    class FixedSQL:
        async def generate(self, **kwargs: Any) -> tuple[str, int]:
            return "SELECT customer_id, country FROM customers", 1

    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    monkeypatch.setenv("TEXT2SQL_PROFILE_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setenv("TEXT2SQL_GLOSSARY_ROOT", str(tmp_path / "glossaries"))
    monkeypatch.setenv("TEXT2SQL_GZIP_ENABLED", "true")
    monkeypatch.setenv("TEXT2SQL_GZIP_MIN_BYTES", "1")
    client = TestClient(
        create_app(TextToSQLAgent(registry, FixedSQL()), run_history=RunHistoryStore(None))
    )
    response = client.post(
        "/api/chat",
        json={
            "session_id": "gzip",
            "db_id": "shop",
            "model": "test-model",
            "message": "Show customers",
            "max_rows": 100,
        },
        headers={"Accept-Encoding": "gzip"},
    )
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert response.json()["generation"]["accepted"] is True


def test_startup_encoder_failure_keeps_bm25_available(
    registry: Any, monkeypatch: Any, tmp_path: Any
) -> None:
    class FixedSQL:
        async def generate(self, **kwargs: Any) -> tuple[str, int]:
            return "SELECT COUNT(*) FROM customers", 1

    def fail_initialization(self: FastEmbedEncoder) -> None:
        raise RuntimeError("No local embedding model")

    monkeypatch.setattr(FastEmbedEncoder, "initialize", fail_initialization)
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    monkeypatch.setenv("TEXT2SQL_PROFILE_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setenv("TEXT2SQL_GLOSSARY_ROOT", str(tmp_path / "glossaries"))
    monkeypatch.setenv("TEXT2SQL_EMBEDDING_PRELOAD", "true")
    app = create_app(TextToSQLAgent(registry, FixedSQL()), run_history=RunHistoryStore(None))
    with TestClient(app) as client:
        response = client.post(
            "/api/chat",
            json={
                "session_id": "dense-failure",
                "db_id": "shop",
                "model": "test-model",
                "message": "Count customers",
            },
        )
    assert response.status_code == 200
    assert response.json()["generation"]["accepted"] is True
