"""Real metadata HTTP/graph tests; no business execution or external inference."""
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_kpi_first import SQLModel, fixture
from test_semantic_graph import join_schema

from semantic_text2sql.agent import TextToSQLAgent
from semantic_text2sql.api import create_app
from semantic_text2sql.database import DatabaseRegistry
from semantic_text2sql.models import ForeignKeyInfo
from semantic_text2sql.semantic_explorer import ExplorerCache
from semantic_text2sql.semantic_graph import compile_graph


@pytest.fixture
def explorer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    # Exercise the same bounded graph pipeline with a synthetic metadata fixture.
    monkeypatch.setattr("semantic_text2sql.semantic_explorer_api.EXPLORER_DB_ID", "shop")
    registry, document = fixture(tmp_path)
    catalog = tmp_path / "catalogs"
    catalog.mkdir()
    path = catalog / "shop.json"
    path.write_text(document.model_dump_json())
    for name, value in {"DATABASE_ROOT": tmp_path, "SEMANTIC_CATALOG_ROOT": catalog,
                        "PROFILE_ROOT": tmp_path / "profiles",
                        "GLOSSARY_ROOT": tmp_path / "glossaries",
                        "GRAPH_ROOT": tmp_path / "graphs"}.items():
        monkeypatch.setenv("TEXT2SQL_" + name, str(value))
    monkeypatch.setenv("TEXT2SQL_GRAPH_ENABLED", "true")
    monkeypatch.setenv("TEXT2SQL_SEMANTIC_EXPLORER_ENABLED", "true")
    monkeypatch.setenv("TEXT2SQL_EMBEDDING_PRELOAD", "false")
    monkeypatch.delenv("TEXT2SQL_CONVERSATION_STORE", raising=False)
    model = SQLModel("SELECT 1")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Explorer attempted a business query")

    monkeypatch.setattr(DatabaseRegistry, "execute", forbidden)
    client = TestClient(create_app(TextToSQLAgent(registry, model)), base_url="http://127.0.0.1",
                        client=("127.0.0.1", 42000))
    return client, registry, document, path, model


def test_debit_only_boundary_on_every_graph_route(
    explorer: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, *_ = explorer
    monkeypatch.setattr("semantic_text2sql.semantic_explorer_api.EXPLORER_DB_ID",
                        "debit_card_specializing")
    assert client.get("/api/semantic-graph/sources").json() == []
    for route in ("?db_id=shop", "/neighborhood?db_id=shop", "/nodes/table:orders?db_id=shop",
                  "/lineage?db_id=shop&node_id=metric:net_revenue&direction=upstream",
                  "/connect?db_id=shop&tables=orders&tables=customers"):
        response = client.get("/api/semantic-graph" + route)
        assert response.status_code == 404, response.text


def test_schema_bounds_determinism_composite_details_and_no_calls(explorer: Any) -> None:
    client, _, _, _, model = explorer
    assert client.get("/api/semantic-graph/sources").json() == ["shop"]
    path = "/api/semantic-graph?db_id=shop&view=schema&depth=0"
    first = client.get(path)
    assert first.status_code == 200, first.text
    assert first.json() == client.get(path).json()
    assert all(node["kind"] == "TABLE" for node in first.json()["nodes"])
    assert first.headers["cache-control"] == "no-store, private"
    detail = client.get("/api/semantic-graph/nodes/table:orders?db_id=shop").json()
    assert detail["columns"] and detail["column_count"] == 6
    assert any(edge["child_columns"] == ["customer_id"] for edge in detail["relationships"])
    assert detail["associated_kpis"]
    text = json.dumps(detail)
    for forbidden in ("approved_by", "approved_at", "content_checksum", "profile_version",
                      "cache", "password", "examples", "top_values", "create_sql"):
        assert forbidden not in text
    assert model.calls == 0


def test_upstream_lineage_and_reverse_potential_impact(explorer: Any) -> None:
    client, *_ = explorer
    root = "/api/semantic-graph/lineage?db_id=shop&view=kpi&depth=6&direction="
    response = client.get(root + "upstream&node_id=metric:net_revenue")
    assert response.status_code == 200, response.text
    body = response.json()
    ids = {node["id"] for node in body["nodes"]}
    assert {"metric:net_revenue", "metric:gross_sales", "metric:refunds", "column:orders.amount",
            "column:orders.refund_amount", "column:orders.status", "table:orders"} <= ids
    assert "table:unrelated" not in ids
    assert body["distances"]["metric:gross_sales"] == 1
    assert not body["potential_impact"]
    impact = client.get(root + "downstream&node_id=column:orders.refund_amount").json()
    assert impact["potential_impact"]
    assert {"metric:refunds", "metric:net_revenue"} <= {node["id"] for node in impact["nodes"]}
    assert "metric:gross_sales" not in {node["id"] for node in impact["nodes"]}


def test_lazy_expansion_search_and_limits(explorer: Any) -> None:
    client, *_ = explorer
    expanded = client.get(
        "/api/semantic-graph/neighborhood?db_id=shop&node_id=table:orders&depth=1").json()
    assert any(node["kind"] == "COLUMN" for node in expanded["nodes"])
    limited = client.get(
        "/api/semantic-graph?db_id=shop&view=combined&node_limit=2&edge_limit=1").json()
    assert len(limited["nodes"]) <= 2 and len(limited["edges"]) <= 1 and limited["truncated"]
    searched = client.get("/api/semantic-graph?db_id=shop&view=kpi&search=refund_amount").json()
    assert any(node["id"] == "column:orders.refund_amount" for node in searched["nodes"])
    assert client.get("/api/semantic-graph?db_id=shop&depth=99").status_code == 422
    assert client.get("/api/semantic-graph?db_id=shop&node_limit=999").status_code == 422
    assert client.get("/api/semantic-graph?db_id=shop&kinds=INVENTED").status_code == 422


@pytest.mark.parametrize("headers", [{"Origin": "https://evil.invalid"},
                                    {"X-Forwarded-For": "127.0.0.1"},
                                    {"Sec-Fetch-Site": "cross-site"}])
def test_local_boundary_cannot_be_bypassed_with_headers(
    explorer: Any, headers: dict[str, str],
) -> None:
    client, *_ = explorer
    assert client.get("/api/semantic-graph/sources", headers=headers).status_code in {401, 403}


def test_unconfigured_and_cross_source_nodes_are_not_revealed(explorer: Any) -> None:
    client, *_ = explorer
    assert client.get("/api/semantic-graph?db_id=outside").status_code == 404
    assert client.get("/api/semantic-graph/nodes/metric:outside?db_id=shop").status_code == 404
    assert client.get(
        "/api/semantic-graph/neighborhood?db_id=shop&node_id=table:outside").status_code == 404


def test_remote_and_disabled_workspace_fail_closed(explorer: Any, monkeypatch: Any) -> None:
    client, *_ = explorer
    remote = TestClient(client.app, base_url="https://shared.invalid", client=("203.0.113.1", 1234))
    assert remote.get("/api/semantic-graph?db_id=shop").status_code == 401
    monkeypatch.setenv("TEXT2SQL_DASHBOARD_LOCAL_WORKSPACE", "false")
    assert client.get("/api/semantic-graph?db_id=shop").status_code == 401
    monkeypatch.setenv("TEXT2SQL_DASHBOARD_LOCAL_WORKSPACE", "true")
    monkeypatch.setenv("TEXT2SQL_SEMANTIC_EXPLORER_ENABLED", "false")
    assert client.get("/api/semantic-graph?db_id=shop").status_code == 503
    assert client.post("/api/semantic-graph?db_id=shop", json={}).status_code == 405


def test_stale_catalog_and_schema_changes_do_not_reuse_old_approved_nodes(explorer: Any) -> None:
    client, registry, document, path, _ = explorer
    endpoint = "/api/semantic-graph?db_id=shop&view=kpi"
    assert client.get(endpoint).json()["nodes"]
    document.schema_fingerprint = "stale"
    path.write_text(document.model_dump_json())
    stale = client.get(endpoint).json()
    assert stale["nodes"] == [] and stale["warnings"]
    assert client.get("/api/semantic-graph/nodes/metric:net_revenue?db_id=shop").status_code == 404
    assert registry.inspect("shop").db_id == "shop"


def test_missing_or_inferred_only_catalog_has_no_approved_kpis(explorer: Any) -> None:
    client, _, document, path, _ = explorer
    path.unlink()
    assert client.get("/api/semantic-graph?db_id=shop&view=kpi").json()["nodes"] == []
    for metric in document.metrics:
        metric.status, metric.approval = "inferred", None
    path.write_text(document.model_dump_json())
    assert client.get("/api/semantic-graph?db_id=shop&view=kpi").json()["nodes"] == []


def test_snapshot_is_compiled_once_until_metadata_changes(explorer: Any, monkeypatch: Any) -> None:
    client, _, document, path, _ = explorer
    from semantic_text2sql import semantic_explorer_api
    original = semantic_explorer_api.compile_graph
    calls = []

    def tracked(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(semantic_explorer_api, "compile_graph", tracked)
    for _ in range(3):
        assert client.get("/api/semantic-graph?db_id=shop").status_code == 200
    assert len(calls) == 1
    document.catalog_version += 1
    path.write_text(document.model_dump_json())
    client.get("/api/semantic-graph?db_id=shop")
    assert len(calls) == 2


def test_join_plan_shows_bridges_and_forbidden_edges_do_not_authorize_paths(explorer: Any) -> None:
    client, _, document, path, _ = explorer
    endpoint = "/api/semantic-graph/connect?db_id=shop&tables=orders&tables=departments"
    result = client.get(endpoint).json()
    assert result["status"] == "CONNECTED" and result["bridges"] == ["customers"]
    assert len(result["edges"]) == 2
    document.relationships[0].forbidden = True
    path.write_text(document.model_dump_json())
    assert client.get(endpoint).json()["status"] == "DISCONNECTED"
    schema = client.get("/api/semantic-graph?db_id=shop&view=schema").json()
    assert any(edge["status"] == "forbidden" for edge in schema["edges"])


def test_composite_keys_and_cycles_retain_bounded_stable_edges() -> None:
    schema = join_schema([("a", "b"), ("b", "a")])
    schema.relationships = [ForeignKeyInfo(from_table="a", from_column=key, to_table="b",
                                          to_column=key, constraint_id="composite", ordinal=i)
                            for i, key in enumerate(["id", "part"])]
    snapshot = compile_graph(schema)
    index = ExplorerCache().get(snapshot, snapshot.network())
    result = index.graph(node_id="table:a", depth=6, node_limit=3)
    assert len(result.nodes) <= 3 and result.truncated
    assert index.graph().edges[0].child_columns == ["id", "part"]
