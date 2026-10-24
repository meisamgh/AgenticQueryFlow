from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Literal

import pytest
from fastapi.testclient import TestClient

from semantic_text2sql.agent import TextToSQLAgent
from semantic_text2sql.api import create_app
from semantic_text2sql.conversation import (
    ConversationConflict,
    ConversationStore,
    classify_operation,
    requires_model_interpretation,
    resolve_semantic_contract,
    resolve_turn,
)
from semantic_text2sql.models import (
    DEFAULT_MODEL,
    ConversationState,
    RequestedChange,
    SchemaInfo,
    SemanticContract,
    SemanticFilter,
    StrategyHints,
    TokenUsage,
)


def test_typed_followup_preserves_unaffected_approved_requirements() -> None:
    accepted = SemanticContract(
        measures=["total_consumption"], aggregation="sum",
        filters=[SemanticFilter(operand="Country", operator="=", value="Germany")],
        required_literals=["Germany"], output_grain=["customer"],
    )
    updated = resolve_semantic_contract(
        accepted,
        original_question="Add currency category",
        resolved_question="Total consumption in Germany by currency",
        changes=[RequestedChange(
            operation="ADD_DIMENSION", target="Currency", new_value="Currency",
            source="model_interpretation",
        )],
    )
    assert updated.measures == ["total_consumption"]
    assert updated.aggregation == "sum"
    assert updated.filters == accepted.filters
    assert updated.required_literals == ["Germany"]
    assert updated.grain == ["Currency"]
    assert updated.output_grain == ["customer", "Currency"]
    assert accepted.grain == []


def test_contract_filter_and_period_changes_preserve_unaffected_constraints() -> None:
    base = SemanticContract(
        filters=[
            SemanticFilter(operand="Date", operator=">=", value="2025-01-01"),
            SemanticFilter(operand="Date", operator="<", value="2026-01-01"),
        ],
        active_period="2025",
        comparison_periods=["2024"],
    )
    updated = resolve_semantic_contract(
        base, original_question="Use 2026", resolved_question="Use 2026",
        changes=[
            RequestedChange(operation="ADD_FILTER", target="Date", new_value="2026-07-01"),
            RequestedChange(operation="CHANGE_PERIOD", target="period", new_value="2026"),
        ],
    )
    assert [(item.operator, item.value) for item in updated.filters] == [
        ("=", "2026-07-01")
    ]
    assert updated.active_period == "2026"
    assert updated.comparison_periods == ["2024"]


def test_active_period_rejects_conflicting_exact_date_filter() -> None:
    base = SemanticContract(active_period="2025")
    with pytest.raises(ValueError, match="active period 2025 conflicts"):
        resolve_semantic_contract(
            base, original_question="Use August 2024", resolved_question="Use August 2024",
            changes=[RequestedChange(
                operation="ADD_FILTER", target="orders.order_date",
                new_value="2024-08-01",
            )],
        )


def test_comparison_period_allows_exact_date_filter_for_other_year() -> None:
    base = SemanticContract(active_period="2025", comparison_periods=["2024"])
    updated = resolve_semantic_contract(
        base, original_question="Compare August 2024", resolved_question="Compare August 2024",
        changes=[RequestedChange(
            operation="ADD_FILTER", target="orders.order_date",
            new_value="2024-08-01",
        )],
    )
    assert updated.filters[0].value == "2024-08-01"


def test_contract_replacement_requires_unique_match() -> None:
    base = SemanticContract(filters=[
        SemanticFilter(operand="Date", operator=">=", value="2025-01-01"),
        SemanticFilter(operand="Date", operator="<", value="2026-01-01"),
    ])
    with pytest.raises(ValueError, match="exactly one"):
        resolve_semantic_contract(
            base, original_question="Change the date", resolved_question="Change the date",
            changes=[RequestedChange(
                operation="REPLACE_FILTER", target="Date", new_value="2026-02-01",
            )],
        )
    updated = resolve_semantic_contract(
        base, original_question="Change the start", resolved_question="Change the start",
        changes=[RequestedChange(
            operation="REPLACE_FILTER", target="Date", previous_value="2025-01-01",
            new_value="2025-02-01",
        )],
    )
    assert [item.value for item in updated.filters] == ["2025-02-01", "2026-01-01"]


def test_contract_metric_change_removes_stale_metric_dependencies() -> None:
    from semantic_text2sql.models import ApprovedMetricDefinition

    base = SemanticContract(
        measures=["revenue"],
        approved_metrics=[ApprovedMetricDefinition(
            id="revenue", formula="SUM(amount)", source_relation="sales",
        )],
    )
    updated = resolve_semantic_contract(
        base, original_question="Use volume", resolved_question="Use volume",
        changes=[RequestedChange(operation="CHANGE_METRIC", target="revenue", new_value="volume")],
    )
    assert updated.measures == ["volume"]
    assert updated.approved_metrics == []


def test_contract_unsupported_delta_is_not_silently_ignored() -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        resolve_semantic_contract(
            SemanticContract(), original_question="Anything", resolved_question="Anything",
            changes=[RequestedChange(operation="OTHER", target="unknown")],
        )


def test_change_period_removes_old_date_bounds_not_country_filter() -> None:
    base = SemanticContract(
        active_period="2025", comparison_periods=["2024"],
        filters=[
            SemanticFilter(operand="orders.order_date", operator=">=", value="2025-01-01"),
            SemanticFilter(operand="orders.order_date", operator="<", value="2026-01-01"),
            SemanticFilter(operand="orders.Country", operator="=", value="Germany"),
        ],
    )
    updated = resolve_semantic_contract(
        base, original_question="Use 2026", resolved_question="Show 2026",
        changes=[RequestedChange(operation="CHANGE_PERIOD", target="year", new_value="2026")],
    )
    assert updated.active_period == "2026"
    assert updated.comparison_periods == ["2024"]
    assert [(item.operand, item.value) for item in updated.filters] == [
        ("orders.Country", "Germany")
    ]


def test_add_conflicting_equality_requires_explicit_intent() -> None:
    base = SemanticContract(filters=[
        SemanticFilter(operand="Country", operator="=", value="Germany"),
    ])
    with pytest.raises(ValueError, match="replacement, comparison, or intersection"):
        resolve_semantic_contract(
            base, original_question="Only France", resolved_question="Only France",
            changes=[RequestedChange(
                operation="ADD_FILTER", target="Country", new_value="France",
            )],
        )


def test_change_metric_clears_old_formula_and_ranking_dependency() -> None:
    base = SemanticContract(
        measures=["revenue"], aggregation="sum", required_columns=["sales.Price"],
        ranked_metric="revenue", required_output_aliases=["revenue"],
        mandatory_metric_filters=[SemanticFilter(operand="status", operator="=", value="paid")],
    )
    updated = resolve_semantic_contract(
        base, original_question="Use volume", resolved_question="Use volume",
        changes=[RequestedChange(operation="CHANGE_METRIC", target="revenue", new_value="volume")],
    )
    assert updated.measures == ["volume"]
    assert updated.aggregation is None
    assert updated.ranked_metric is None
    assert updated.required_columns == []
    assert updated.mandatory_metric_filters == []


def test_explicit_filter_value_correction_replaces_stored_question() -> None:
    previous = ConversationState(
        session_id="replacement",
        db_id="debit_card_specializing",
        root_question="Compare OOK and LAM customers paid in Dollar.",
        resolved_question="Compare OOK and LAM customers paid in Dollar.",
        turn_count=1,
    )

    operation, state = resolve_turn(
        "replacement",
        "debit_card_specializing",
        'Replace filter value "OOK" with "SME".',
        previous,
        feedback_category="missing_filter",
    )

    assert operation == "CORRECTION"
    assert state is not None
    assert state.root_question == "Compare SME and LAM customers paid in Dollar."
    assert state.resolved_question == "Compare SME and LAM customers paid in Dollar."
    assert state.corrections == ["Replaced filter value 'OOK' with 'SME'."]


class ConversationModel:
    def __init__(self) -> None:
        self.responses = [
            "SELECT customer_id, COUNT(*) FROM orders GROUP BY customer_id",
            "SELECT o.customer_id, COUNT(*) FROM orders o JOIN customers c "
            "ON o.customer_id = c.customer_id WHERE c.country = 'Germany' "
            "GROUP BY o.customer_id",
            "SELECT customer_id, COUNT(*) FROM orders GROUP BY customer_id",
            "SELECT o.customer_id, COUNT(*) FROM orders o JOIN customers c "
            "ON o.customer_id = c.customer_id WHERE c.country = 'Germany' "
            "GROUP BY o.customer_id",
            "SELECT o.customer_id, COUNT(*) + 1 FROM orders o JOIN customers c "
            "ON o.customer_id = c.customer_id WHERE c.country = 'Germany' "
            "GROUP BY o.customer_id",
            "SELECT o.customer_id, COUNT(*) FROM orders o JOIN customers c "
            "ON o.customer_id = c.customer_id WHERE c.country = 'Germany' "
            "GROUP BY o.customer_id",
        ]
        self.previous_sql: list[str | None] = []

    async def generate(
        self,
        *,
        provider: Literal["agentrouter", "groq"],
        model: str,
        question: str,
        evidence: str | None,
        schema: SchemaInfo,
        strategy: StrategyHints,
        dialect: Literal["sqlite", "postgres"],
        profile_context: str,
        previous_sql: str | None,
        feedback: str | None,
        rejected_shapes: list[str],
        generation_style: Literal["reasoning", "icl", "alternative"],
    ) -> tuple[str, int]:
        self.previous_sql.append(previous_sql)
        return self.responses.pop(0), 1


class TurnModel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def complete_detailed(self, model: str, prompt: str):  # type: ignore[no-untyped-def]
        self.calls.append((model, prompt))
        return (
            json.dumps(
                {
                    "operation": "CORRECTION",
                    "depends_on_previous": True,
                    "resolved_instruction": (
                        "Calculate the average per customer before selecting the minimum."
                    ),
                    "correction_type": "wrong_aggregation",
                    "confidence": 0.96,
                }
            ),
            TokenUsage(),
        )


class UncertainTurnModel:
    async def complete_detailed(self, model: str, prompt: str):  # type: ignore[no-untyped-def]
        return (
            json.dumps(
                {
                    "operation": "REFINE",
                    "depends_on_previous": True,
                    "resolved_instruction": "Use that solution.",
                    "correction_type": None,
                    "confidence": 0.4,
                }
            ),
            TokenUsage(input_tokens=10, output_tokens=5),
        )


class ExplanationModel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def complete_detailed(self, model: str, prompt: str):  # type: ignore[no-untyped-def]
        self.calls.append((model, prompt))
        return (
            "The query joins orders to customers to apply the country filter.",
            TokenUsage(input_tokens=20, output_tokens=12),
        )


def test_classifier_detects_correction_and_explanation() -> None:
    assert classify_operation("That's wrong. Only paying customers", True) == "CORRECTION"
    assert classify_operation("Use only paying customers", True, "missing_filter") == "CORRECTION"
    assert classify_operation("Why did you use the orders table?", True) == "EXPLAIN_CONTEXT"
    assert classify_operation("Why did you use LEFT JOIN?", True) == "EXPLAIN_SQL"
    assert classify_operation("Can you explain why LEFT JOIN was used?", True) == "EXPLAIN_SQL"
    assert classify_operation("What does this query do?", True) == "EXPLAIN_SQL"
    assert classify_operation("Why are there duplicate rows?", True) == "EXPLAIN_RESULT"
    assert classify_operation("Why did the query fail?", True) == "EXPLAIN_FAILURE"
    assert (
        classify_operation("Explain how you interpreted my request", True)
        == "EXPLAIN_INTERPRETATION"
    )
    assert classify_operation("Optimize it", True) == "OPTIMIZE"
    assert classify_operation("Check whether this SQL is correct", True) == "CHECK_CORRECTNESS"
    assert requires_model_interpretation(
        "You need to calculate average per customer before selecting the minimum", True
    )
    assert not requires_model_interpretation("Optimize it", True)


def test_ambiguous_turn_uses_client_selected_model(registry, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    monkeypatch.setenv("TEXT2SQL_GLOSSARY_ROOT", str(registry.root / "glossaries"))
    sql_model = ConversationModel()
    turn_model = TurnModel()
    client = TestClient(
        create_app(
            TextToSQLAgent(registry, sql_model),
            conversation_completers={"agentrouter": turn_model},
        )
    )
    base = {
        "session_id": "model-resolver",
        "db_id": "shop",
        "provider": "agentrouter",
        "model": DEFAULT_MODEL,
        "execute": True,
    }

    first = client.post(
        "/api/chat", json={**base, "message": "How many orders for each customer?"}
    ).json()
    corrected = client.post(
        "/api/chat",
        json={
            **base,
            "message": "You need to calculate average per customer before selecting the minimum",
        },
    ).json()

    assert first["conversation_interpretation"]["source"] == "rules"
    assert corrected["operation"] == "CORRECTION"
    assert corrected["conversation_interpretation"] == {
        "operation": "CORRECTION",
        "depends_on_previous": True,
        "resolved_instruction": (
            "Calculate the average per customer before selecting the minimum."
        ),
        "correction_type": "wrong_aggregation",
        "target": None,
        "confidence": 0.96,
        "source": "model",
        "provider": "agentrouter",
        "model": DEFAULT_MODEL,
        "changes": [],
    }
    assert turn_model.calls[0][0] == DEFAULT_MODEL
    assert any("Previous accepted SQL" in prompt for _, prompt in turn_model.calls)
    assert "Correction category=wrong_aggregation" in corrected["resolved_question"]
    assert "Calculate the average per customer" in corrected["resolved_question"]


def test_low_confidence_turn_asks_for_clarification(registry, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    sql_model = ConversationModel()
    uncertain = UncertainTurnModel()
    client = TestClient(
        create_app(
            TextToSQLAgent(registry, sql_model),
            conversation_completers={"agentrouter": uncertain},
        )
    )
    base = {
        "session_id": "uncertain-resolver",
        "db_id": "shop",
        "provider": "agentrouter",
        "model": DEFAULT_MODEL,
        "execute": True,
    }
    client.post("/api/chat", json={**base, "message": "How many orders per customer?"})
    response = client.post("/api/chat", json={**base, "message": "Use that solution"}).json()

    assert response["clarification_required"] is True
    assert response["generation"] is None
    assert response["state"]["turn_count"] == 1
    assert response["token_usage"]["input_tokens"] == 10


def test_chat_preserves_adds_removes_and_resets_context(registry, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    monkeypatch.setenv("TEXT2SQL_GLOSSARY_ROOT", str(registry.root / "glossaries"))
    explanation_model = ExplanationModel()
    client = TestClient(
        create_app(
            TextToSQLAgent(registry, ConversationModel()),
            conversation_completers={
                "agentrouter": explanation_model,
            },
        )
    )
    base = {
        "session_id": "conversation-1",
        "db_id": "shop",
        "provider": "agentrouter",
        "model": "test-model",
        "execute": True,
    }

    first = client.post(
        "/api/chat", json={**base, "message": "How many orders for each customer?"}
    ).json()
    second = client.post(
        "/api/chat", json={**base, "message": "Now only customers in Germany"}
    ).json()
    third = client.post(
        "/api/chat", json={**base, "message": "Now remove the Germany filter"}
    ).json()
    correction = client.post(
        "/api/chat",
        json={
            **base,
            "message": "That is wrong; only Germany should be included",
            "feedback_category": "missing_filter",
        },
    ).json()
    optimized = client.post("/api/chat", json={**base, "message": "Optimize it"}).json()
    explanation = client.post(
        "/api/chat", json={**base, "message": "Why did you use these tables?"}
    ).json()
    reset = client.post("/api/chat", json={**base, "message": "Reset context"}).json()

    assert first["operation"] == "NEW_QUERY"
    assert first["generation"]["rows"] == [[1, 2], [2, 1]]
    assert second["operation"] == "ADD_FILTER"
    assert second["generation"]["rows"] == [[1, 2]]
    assert second["state"]["modifications"] == ["Now only customers in Germany"]
    assert second["state"]["contract_deltas"] == [
        {
            "operation": "ADD_FILTER",
            "instruction": "Now only customers in Germany",
            "feedback_category": None,
            "changes": [],
        }
    ]
    assert third["operation"] == "REMOVE_FILTER"
    assert third["generation"]["rows"] == [[1, 2], [2, 1]]
    assert "later instructions override" in third["resolved_question"]
    assert correction["operation"] == "CORRECTION"
    assert correction["generation"]["rows"] == [[1, 2]]
    assert correction["state"]["corrections"] == [
        "Correction category=missing_filter: That is wrong; only Germany should be included"
    ]
    assert optimized["operation"] == "OPTIMIZE"
    assert optimized["generation"]["accepted"] is True
    assert optimized["generation"]["sql"]
    assert optimized["generation"]["optimization"] is not None
    assert optimized["generation"]["attempts"][1]["validation"]["code"] == (
        "OPTIMIZATION_NOT_FASTER"
    )
    assert optimized["generation"]["optimization"]["status"] == "equivalent_not_faster"
    assert optimized["generation"]["optimization"]["selected_sql"] == "baseline"
    assert explanation["operation"] == "EXPLAIN_CONTEXT"
    assert explanation["generation"] is None
    assert explanation["explanation"] == (
        "The query joins orders to customers to apply the country filter."
    )
    assert any("VERIFIED_SQL_FACTS" in prompt for _, prompt in explanation_model.calls)
    assert explanation["token_usage"]["input_tokens"] == 20
    assert reset["operation"] == "RESET_CONTEXT"
    assert reset["state"] is None


def test_stateful_action_without_previous_query_requests_clarification(
    registry, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    client = TestClient(create_app(TextToSQLAgent(registry, ConversationModel())))
    base = {
        "session_id": "missing-state",
        "db_id": "shop",
        "provider": "agentrouter",
        "model": "test-model",
        "execute": True,
    }

    optimize = client.post("/api/chat", json={**base, "message": "Optimize it"}).json()
    explain = client.post(
        "/api/chat", json={**base, "message": "Why did you use LEFT JOIN?"}
    ).json()

    assert optimize["operation"] == "OPTIMIZE"
    assert optimize["clarification_required"] is True
    assert "previous accepted query" in optimize["message"]
    assert explain["operation"] == "EXPLAIN_SQL"
    assert explain["clarification_required"] is True
    assert "previous query" in explain["message"]


def test_conversation_store_is_durable_versioned_and_detects_stale_writes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "conversations.sqlite3"
    store = ConversationStore(path)
    initial = ConversationState(
        session_id="durable",
        db_id="shop",
        root_question="Count orders",
        resolved_question="Count orders",
    )

    saved = store.put(initial)
    reloaded = ConversationStore(path).get("durable")

    assert saved.version == 1
    assert reloaded is not None
    assert reloaded.version == 1
    with pytest.raises(ConversationConflict):
        store.put(initial, expected_version=None)


def test_conversation_store_removes_expired_state(tmp_path: Path) -> None:
    path = tmp_path / "conversations.sqlite3"
    store = ConversationStore(path, ttl_seconds=60)
    store.put(
        ConversationState(
            session_id="expired",
            db_id="shop",
            root_question="Count orders",
            resolved_question="Count orders",
        )
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE conversations SET updated_at = ? WHERE session_id = ?",
            (time.time() - 61, "expired"),
        )

    assert store.get("expired") is None


class CorrectnessModel:
    """An accepted query, then one independent review candidate per configured SQL."""

    def __init__(self, review_sql: str) -> None:
        self.queries = iter(
            [
                "SELECT customer_id, COUNT(*) FROM orders GROUP BY customer_id",
                review_sql,
            ]
        )

    async def generate(self, **kwargs: Any) -> tuple[str, int]:
        return next(self.queries), 1


def _correctness_client(
    registry, monkeypatch, review_sql: str, session_id: str, extra_env: dict[str, str] | None = None
) -> tuple[TestClient, dict[str, object]]:
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    for key, value in (extra_env or {}).items():
        monkeypatch.setenv(key, value)
    client = TestClient(create_app(TextToSQLAgent(registry, CorrectnessModel(review_sql))))
    base: dict[str, object] = {
        "session_id": session_id,
        "db_id": "shop",
        "provider": "agentrouter",
        "model": "test-model",
        "execute": True,
    }
    client.post("/api/chat", json={**base, "message": "How many orders for each customer?"})
    return client, base


def test_correctness_check_reruns_prior_sql_and_confirms_equivalence(
    registry, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    client, base = _correctness_client(
        registry,
        monkeypatch,
        "SELECT customer_id, COUNT(*) FROM orders GROUP BY customer_id",
        "correctness-equivalent",
    )
    response = client.post(
        "/api/chat", json={**base, "message": "Please verify correctness"}
    ).json()

    assert response["operation"] == "CHECK_CORRECTNESS"
    assert response["human_review"] is None
    assert "same bounded result" in response["message"]


def test_correctness_check_reports_disagreement_as_human_review(
    registry, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    client, base = _correctness_client(
        registry,
        monkeypatch,
        "SELECT o.customer_id, COUNT(*) + 1 FROM orders o JOIN customers c "
        "ON o.customer_id = c.customer_id GROUP BY o.customer_id",
        "correctness-disagreement",
    )
    response = client.post(
        "/api/chat", json={**base, "message": "Please verify correctness"}
    ).json()

    assert response["operation"] == "CHECK_CORRECTNESS"
    assert response["human_review"] is not None
    assert "did not reproduce" in response["human_review"]["reason"]


def test_correctness_check_without_budget_reports_unverified_review(
    registry, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    client, base = _correctness_client(
        registry,
        monkeypatch,
        "SELECT customer_id, COUNT(*) FROM orders GROUP BY customer_id",
        "correctness-no-budget",
        # The request-scoped schema snapshot removed one database inspection.
        # Two calls cover schema inspection and candidate execution, leaving
        # none for the independent equivalence re-execution.
        extra_env={"TEXT2SQL_REQUEST_MAX_DATABASE_CALLS": "2"},
    )
    response = client.post(
        "/api/chat", json={**base, "message": "Please verify correctness"}
    ).json()

    assert response["operation"] == "CHECK_CORRECTNESS"
    assert response["human_review"] is not None
    assert "unverified" in response["human_review"]["reason"]
    assert "did not reproduce" not in response["human_review"]["reason"]
