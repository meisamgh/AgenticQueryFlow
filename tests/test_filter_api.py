"""Bounded filter-values endpoint and chat-response discovery wiring."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from semantic_text2sql.agent import TextToSQLAgent
from semantic_text2sql.api import create_app
from semantic_text2sql.filter_discovery import applied_sql_values
from semantic_text2sql.models import SchemaInfo
from semantic_text2sql.postgres import PostgresRegistry


class FixedSQLModel:
    def __init__(self, sql: str) -> None:
        self.sql = sql

    async def generate(self, **kwargs: Any) -> tuple[str, int]:
        return self.sql, 1


def test_applied_filter_values_come_from_sql_not_sample_profile(registry) -> None:  # type: ignore[no-untyped-def]
    schema = registry.inspect("shop")
    assert applied_sql_values(
        "SELECT c.country FROM customers AS c "
        "WHERE c.country IN ('Germany', 'Italy') AND c.country != 'Spain'",
        schema,
    ) == {"customers.country": ["Germany", "Italy"]}
    assert applied_sql_values("SELECT country FROM customers WHERE country = 1", schema) == {}
    assert applied_sql_values(
        "SELECT country FROM customers WHERE country NOT IN ('Germany')", schema
    ) == {}
    assert applied_sql_values(
        "SELECT country FROM customers WHERE country = 'Germany' OR country = 'Italy'", schema
    ) == {}


def _client(registry, monkeypatch, tmp_path, sql=None):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    monkeypatch.setenv("TEXT2SQL_PROFILE_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setenv("TEXT2SQL_GLOSSARY_ROOT", str(tmp_path / "glossaries"))
    return TestClient(create_app(TextToSQLAgent(registry, FixedSQLModel(
        sql or "SELECT country, COUNT(*) AS customer_count FROM customers GROUP BY country"
    ))))


def test_chat_filter_catalog_preselects_accepted_sql_literal(
    registry, monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    client = _client(registry, monkeypatch, tmp_path,
                     "SELECT country FROM customers WHERE country = 'Germany'")
    response = client.post("/api/chat", json={
        "session_id": "applied-filter-session", "db_id": "shop",
        "provider": "agentrouter", "model": "test-model",
        "message": "Show customers in Germany.",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["generation"]["accepted"] is True
    assert body["suggested_filters"]["applied_values"] == {"customers.country": ["Germany"]}


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
    assert body["complete_domain"] is True
    assert body["provenance"] == "LIVE_BOUNDED_QUERY"
    second = client.get("/api/databases/shop/filter-values", params={"table": "customers",
                                                                     "column": "country"})
    assert second.status_code == 200
    assert second.json()["cached"] is True


def test_exact_category_verification_and_cache_expiration(
    registry, monkeypatch, tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    import semantic_text2sql.api as api

    now = [100.0]
    monkeypatch.setattr(api, "monotonic", lambda: now[0])
    client = _client(registry, monkeypatch, tmp_path)
    endpoint = "/api/databases/shop/filter-values"
    params = {"table": "customers", "column": "country", "search": "germany", "exact": True}
    first = client.get(endpoint, params=params).json()
    assert first["values"] == ["Germany"]
    assert not first["ambiguous"]
    assert client.get(endpoint, params=params).json()["cached"]
    now[0] += 61
    assert not client.get(endpoint, params=params).json()["cached"]
    params["search"] = "Germ"
    assert client.get(endpoint, params=params).json()["values"] == []
    params["exact"] = False
    assert client.get(endpoint, params=params).json()["values"] == ["Germany"]


def test_filter_search_treats_quotes_and_wildcards_as_literal_text(
    registry, monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    client = _client(registry, monkeypatch, tmp_path)
    endpoint = "/api/databases/shop/filter-values"
    base = {"table": "customers", "column": "country"}
    for search, expected in (
        ("Ger", ["Germany"]),
        ("' OR 1=1 --", []),
        ("%", []),
        ("_", []),
    ):
        response = client.get(endpoint, params={**base, "search": search})
        assert response.status_code == 200
        assert response.json()["values"] == expected


def test_postgres_filter_search_passes_literal_as_bound_parameter(
    registry, monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv(
        "TEXT2SQL_POSTGRES_DATABASES", '{"finance":"postgresql://unused/finance"}'
    )
    schema = SchemaInfo.model_validate({
        "db_id": "finance", "dialect": "postgres",
        "tables": [{
            "name": "customers", "create_sql": "CREATE TABLE customers (country text)",
            "columns": [{"name": "country", "data_type": "text", "primary_key": False}],
        }],
    })
    observed: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(PostgresRegistry, "inspect", lambda self, db_id: schema)

    def execute(self, db_id, sql, *, max_rows, timeout_seconds, parameters=()):  # type: ignore[no-untyped-def]
        observed.append((sql, parameters))
        return ["country"], [], False

    monkeypatch.setattr(PostgresRegistry, "execute", execute)
    client = _client(registry, monkeypatch, tmp_path)
    search = "' OR 1=1 -- %_"
    response = client.get("/api/databases/finance/filter-values", params={
        "table": "customers", "column": "country", "search": search,
    })
    assert response.status_code == 200
    sql, parameters = observed[0]
    assert "strpo" in sql and "%s" in sql
    assert search not in sql
    assert parameters == (search,)


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


def test_chart_followup_reuses_complete_rows_but_filter_change_requeries(
    registry, monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    client = _client(registry, monkeypatch, tmp_path)
    base = {
        "session_id": "chart-followup-session", "db_id": "shop",
        "provider": "agentrouter", "model": "test-model",
    }
    first = client.post("/api/chat", json={**base, "message": "Customer counts by country."})
    assert first.status_code == 200
    assert first.json()["generation"]["accepted"] is True

    reuse = client.post("/api/chat", json={**base, "message": "Plot it."})
    assert reuse.status_code == 200
    assert reuse.json()["operation"] == "VISUALIZE"
    assert "without running SQL again" in reuse.json()["message"]

    changed = client.post(
        "/api/chat", json={**base, "message": "Now plot it only for Germany."}
    )
    assert changed.status_code == 200
    assert "without running SQL again" not in changed.json()["message"]
    assert changed.json()["generation"] is not None


def test_chart_primary_recommendation_matches_displayed_chart(
    registry, monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    client = _client(registry, monkeypatch, tmp_path)
    response = client.post("/api/chat", json={
        "session_id": "chart-primary-session", "db_id": "shop",
        "provider": "agentrouter", "model": "test-model",
        "message": "Plot customer counts by country.",
    })
    assert response.status_code == 200
    body = response.json()
    primary = body["chart_recommendations"]["primary_chart"]
    displayed = body["presentation"]["chart_spec"]
    assert displayed["chart_type"] == primary["chart_type"]
    assert displayed["x"] == primary["x"]
    assert displayed["y"] == primary["y"]
