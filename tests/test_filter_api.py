"""Bounded filter-values endpoint and chat-response discovery wiring."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from semantic_text2sql.agent import TextToSQLAgent
from semantic_text2sql.api import create_app


class FixedSQLModel:
    def __init__(self, sql: str) -> None:
        self.sql = sql

    async def generate(self, **kwargs: Any) -> tuple[str, int]:
        return self.sql, 1


def _client(registry, monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    monkeypatch.setenv("TEXT2SQL_PROFILE_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setenv("TEXT2SQL_GLOSSARY_ROOT", str(tmp_path / "glossaries"))
    return TestClient(create_app(TextToSQLAgent(registry, FixedSQLModel(
        "SELECT country, COUNT(*) AS customer_count FROM customers GROUP BY country"
    ))))


def test_filter_values_returns_bounded_distinct_values(
    registry, monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    client = _client(registry, monkeypatch, tmp_path)
    first = client.get("/api/databases/shop/filter-values", params={"table": "customers",
                                                                     "column": "country"})
    assert first.status_code == 200
    body = first.json()
    assert body["values"] == ["Germany", "Italy"]
    assert body["more_available"] is False
    assert body["cached"] is False
    second = client.get("/api/databases/shop/filter-values", params={"table": "customers",
                                                                     "column": "country"})
    assert second.status_code == 200
    assert second.json()["cached"] is True


def test_filter_values_rejects_unsafe_fields(
    registry, monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    client = _client(registry, monkeypatch, tmp_path)
    assert client.get("/api/databases/shop/filter-values",
                      params={"table": "customers", "column": "customer_id"}).status_code == 404
    assert client.get("/api/databases/shop/filter-values",
                      params={"table": "customers", "column": "amount"}).status_code == 404
    valid = client.get("/api/databases/shop/filter-values",
                       params={"table": "orders", "column": "status"})
    assert valid.status_code == 200
    assert valid.json()["values"] == ["complete", "pending"]
    assert client.get("/api/databases/shop/filter-values",
                      params={"table": "orders; DROP--", "column": "status"}).status_code == 400


def test_chat_response_carries_recommendations_and_filters(
    registry, monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    client = _client(registry, monkeypatch, tmp_path)
    response = client.post("/api/chat", json={
        "session_id": "filter-session",
        "db_id": "shop",
        "provider": "agentrouter",
        "model": "test-model",
        "message": "Customer counts by country.",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["generation"]["accepted"] is True
    recommendations = body.get("chart_recommendations")
    assert recommendations is not None
    assert recommendations["primary_chart"]["x"] == "country"
    filters = body.get("suggested_filters")
    assert filters is not None
    client_fields = {item["field"] for item in filters["client_filters"]}
    assert "country" in client_fields
    assert all(item["exec_mode"] == "CLIENT_FILTER" for item in filters["client_filters"])
    assert all(item["source"] == "stored_result" for item in filters["client_filters"])
    sql_fields = {(item["field"], item["exec_mode"]) for item in filters["sql_filters"]}
    assert ("customers.name", "SQL_FILTER") in sql_fields


def test_measures_never_appear_as_client_filters(
    registry, monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    client = _client(registry, monkeypatch, tmp_path)
    response = client.post("/api/chat", json={
        "session_id": "measure-session",
        "db_id": "shop",
        "provider": "agentrouter",
        "model": "test-model",
        "message": "Customer counts by country.",
    })
    body = response.json()
    filters = body["suggested_filters"]
    assert "customer_count" not in {item["field"] for item in filters["client_filters"]}
