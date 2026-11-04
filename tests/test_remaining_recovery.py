"""Recovery audit must preserve failed repairs, not just successful results."""
from typing import Any

import pytest
from fastapi.testclient import TestClient

from semantic_text2sql.agent import TextToSQLAgent
from semantic_text2sql.api import create_app
from semantic_text2sql.models import RecoveryTrace, TokenUsage
from semantic_text2sql.recovery import RecoveryCoordinator


def test_failed_post_result_repair_preserves_attempts_and_tokens(
    registry: Any, monkeypatch: Any, tmp_path: Any,
) -> None:
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    monkeypatch.setenv("TEXT2SQL_PROFILE_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setenv("TEXT2SQL_GLOSSARY_ROOT", str(tmp_path / "glossaries"))
    monkeypatch.setenv("TEXT2SQL_CONVERSATIONAL_AGENT_ENABLED", "false")
    monkeypatch.setenv("TEXT2SQL_CONVERSATIONAL_BRIDGE_ENABLED", "false")

    async def review(*args: Any, **kwargs: Any) -> RecoveryTrace:
        return RecoveryTrace(failure_code="ZERO_RESULT", failure_category="data_grounding",
                             agent_action="REPAIR", repair_instruction="Correct the query safely.")

    monkeypatch.setattr(RecoveryCoordinator, "ainvestigate", review)

    class Model:
        calls = 0

        async def generate(self, **kwargs: Any) -> tuple[str, int, TokenUsage]:
            self.calls += 1
            return (
                "SELECT customer_id FROM customers WHERE customer_id=-1"
                if self.calls == 1 else "DELETE FROM customers",
                1, TokenUsage(input_tokens=10, output_tokens=5),
            )

    model = Model()
    client = TestClient(create_app(TextToSQLAgent(registry, model)))
    response = client.post("/api/chat", json={
        "session_id": "failed-repair", "db_id": "shop", "message": "List customers",
        "provider": "agentrouter", "model": "scripted",
    })
    assert response.status_code == 200
    generated = response.json()["generation"]
    assert not generated["accepted"] and generated["rows"] == []
    assert len(generated["attempts"]) == model.calls == 2
    assert generated["token_usage"]["input_tokens"] == 20
    assert generated["token_usage"]["output_tokens"] == 10
    assert generated["recovery"]["repair_execution_succeeded"] is False
    assert generated["recovery"]["false_repair"] is None
    assert sum(item["component"] in {"sql_generation", "sql_repair"}
               for item in response.json()["call_ledger"]) == 2
    assert registry.execute("shop", "SELECT COUNT(*) FROM customers", max_rows=1)[1] == [[2]]


def test_proven_semantic_false_repair_is_recorded_without_accepting_result(
    registry: Any, monkeypatch: Any, tmp_path: Any,
) -> None:
    import asyncio

    from test_conversational_bridge import SmallEncoder

    from semantic_text2sql.glossary import GlossaryStore
    from semantic_text2sql.historical import HistoricalQueryStore
    from semantic_text2sql.hybrid_retrieval import HybridSchemaRetriever
    from semantic_text2sql.models import SemanticContract
    from semantic_text2sql.profiling import ProfileStore
    from semantic_text2sql.service import TextToSQLService

    async def review(*args: Any, **kwargs: Any) -> RecoveryTrace:
        return RecoveryTrace(failure_code="NULL_RESULT", failure_category="data_grounding",
                             agent_action="REPAIR", repair_instruction="Preserve SUM semantics.")

    monkeypatch.setattr(RecoveryCoordinator, "ainvestigate", review)

    class Model:
        calls = 0

        async def generate(self, **kwargs: Any) -> tuple[str, int, TokenUsage]:
            self.calls += 1
            return ("SELECT SUM(amount) FROM orders WHERE amount<0" if self.calls == 1
                    else "SELECT AVG(amount) FROM orders", 1,
                    TokenUsage(input_tokens=10, output_tokens=5))

    model = Model()
    service = TextToSQLService(
        database=registry, profiles=ProfileStore(tmp_path / "profiles"),
        glossaries=GlossaryStore(tmp_path / "glossaries"), history=HistoricalQueryStore(),
        retriever=HybridSchemaRetriever(SmallEncoder()), agent=TextToSQLAgent(registry, model))
    outcome = asyncio.run(service.execute_question(
        question="Sum orders amount", db_id="shop", provider="agentrouter", model="scripted",
        semantic_contract=SemanticContract(aggregation="sum", measures=["orders.amount"])))
    result = outcome.generated
    assert not result.accepted and len(result.attempts) == model.calls == 2
    assert result.recovery is not None and result.recovery.false_repair is True
    assert result.recovery.repair_execution_succeeded is False
    assert result.token_usage.input_tokens == 20


@pytest.mark.parametrize("drift", [False, True])
def test_ontology_definition_enters_governed_contract_and_rejects_midturn_drift(
    monkeypatch: Any, tmp_path: Any, drift: bool,
) -> None:
    import asyncio

    from test_conversational_bridge import SmallEncoder
    from test_semantic_catalog import _registry
    from test_semantic_graph import approved_document

    from semantic_text2sql.glossary import GlossaryStore
    from semantic_text2sql.graph_grounding import resolve_metrics
    from semantic_text2sql.historical import HistoricalQueryStore
    from semantic_text2sql.hybrid_retrieval import HybridSchemaRetriever
    from semantic_text2sql.models import SemanticContract, SemanticFilter
    from semantic_text2sql.profiling import ProfileStore
    from semantic_text2sql.semantic_catalog import SemanticCatalogStore
    from semantic_text2sql.service import TextToSQLService

    registry = _registry(tmp_path)
    document = approved_document()
    document.metrics[0].mandatory_filters = [SemanticFilter(
        operand="orders.status", operator="=", value="complete")]
    catalog_root = tmp_path / "catalogs"
    catalog_root.mkdir()
    path = catalog_root / "shop.json"
    path.write_text(document.model_dump_json())
    approved, _ = resolve_metrics(
        "Order total", registry.inspect("shop"), document, SemanticContract())

    async def review(*args: Any, **kwargs: Any) -> RecoveryTrace:
        if drift:
            document.metrics[0].unit = "different-unit"
            path.write_text(document.model_dump_json())
        return RecoveryTrace(failure_code="CORRECTNESS_REVIEW", failure_category="correctness",
                             agent_action="REPAIR",
                             repair_instruction="Use the approved definition.",
                             ontology_metrics=approved.approved_metrics)

    monkeypatch.setattr(RecoveryCoordinator, "ainvestigate", review)

    class Model:
        calls = 0

        async def generate(self, **kwargs: Any) -> tuple[str, int]:
            self.calls += 1
            assert "SUM(amount)" in kwargs["profile_context"]
            assert "complete" in kwargs["profile_context"]
            return "SELECT SUM(amount) AS order_total FROM orders WHERE status='complete'", 1

    model = Model()
    service = TextToSQLService(
        database=registry, profiles=ProfileStore(tmp_path / "profiles"),
        glossaries=GlossaryStore(tmp_path / "glossaries"), history=HistoricalQueryStore(),
        retriever=HybridSchemaRetriever(SmallEncoder()), agent=TextToSQLAgent(registry, model),
        semantic_catalogs=SemanticCatalogStore(catalog_root))
    outcome = asyncio.run(service.execute_question(
        question="Check the previous result", db_id="shop",
        provider="agentrouter", model="scripted",
        correctness_review=True, previous_sql="SELECT AVG(amount) FROM orders",
        previous_approved_tables=["orders"], max_attempts=1))
    if drift:
        assert not outcome.generated.accepted and model.calls == 0
        assert outcome.generated.termination_reason == "context_error"
    else:
        assert outcome.generated.accepted and outcome.generated.rows == [[150.0]]
        assert outcome.semantic_contract.approved_metrics[0].catalog_identity
        assert outcome.semantic_contract.mandatory_metric_filters[0].value == "complete"
        assert model.calls == 1
