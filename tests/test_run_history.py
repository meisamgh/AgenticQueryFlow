from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from semantic_text2sql.agent import TextToSQLAgent
from semantic_text2sql.api import create_app
from semantic_text2sql.models import TokenUsage
from semantic_text2sql.run_history import RunHistoryStore


class CountingModel:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, **kwargs: Any) -> tuple[str, int, TokenUsage]:
        self.calls += 1
        return (
            "SELECT customer_id, COUNT(*) FROM orders GROUP BY customer_id",
            1,
            TokenUsage(input_tokens=10, output_tokens=5),
        )


def test_reopen_restore_branch_and_rerun_survive_api_restart(
    registry,
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    monkeypatch.setenv("TEXT2SQL_GLOSSARY_ROOT", str(tmp_path / "glossaries"))
    monkeypatch.setenv("TEXT2SQL_PROFILE_ROOT", str(tmp_path / "profiles"))
    path = tmp_path / "history.sqlite"
    model = CountingModel()
    client = TestClient(
        create_app(TextToSQLAgent(registry, model), run_history=RunHistoryStore(path))
    )
    base = {"session_id": "owner", "db_id": "shop", "model": "test", "execute": True}
    first = client.post("/api/chat", json={**base, "message": "Count orders per customer"}).json()
    second = client.post(
        "/api/chat",
        json={
            **base,
            "message": "Now only Germany",
            "parent_run_id": first["run_id"],
        },
    ).json()
    assert first["history_saved"] and second["history_saved"]
    assert second["conversation_id"] == first["conversation_id"]
    assert second["parent_run_id"] == first["run_id"]
    assert second["sql_version"] == 1  # The model returned unchanged SQL.

    # A new app loses the in-memory accepted state; history alone restores it.
    client = TestClient(
        create_app(TextToSQLAgent(registry, model), run_history=RunHistoryStore(path))
    )
    calls_before_reopen = model.calls
    reopened = client.get(f"/api/history/{second['run_id']}/turns", params={"session_id": "owner"})
    assert [turn["run_id"] for turn in reopened.json()] == [first["run_id"], second["run_id"]]
    assert reopened.json()[0]["response_payload"]["generation"]["rows"] == [[1, 2], [2, 1]]
    assert reopened.json()[0]["response_payload"]["generation"]["attempts"]
    assert reopened.json()[0]["response_payload"]["generation"]["model_context"]
    assert model.calls == calls_before_reopen
    assert (
        client.get(f"/api/history/{first['run_id']}", params={"session_id": "other"}).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/history/{first['run_id']}/continue", params={"session_id": "other"}
        ).status_code
        == 404
    )

    restored = client.post(
        f"/api/history/{first['run_id']}/continue", params={"session_id": "owner"}
    )
    assert restored.json()["modifications"] == []
    assert model.calls == calls_before_reopen
    branch = client.post(
        "/api/chat",
        json={
            **base,
            "message": "Now exclude Italy",
            "parent_run_id": first["run_id"],
        },
    ).json()
    assert branch["parent_run_id"] == first["run_id"]
    assert branch["conversation_id"] == first["conversation_id"]
    assert "Germany" not in branch["state"]["resolved_question"]
    assert "Italy" in branch["state"]["resolved_question"]
    # API clients can also choose a parent explicitly without a separate restore call.
    another_branch = client.post("/api/chat", json={
        **base, "message": "Now only Germany", "parent_run_id": first["run_id"],
    }).json()
    assert "Italy" not in another_branch["state"]["resolved_question"]

    # An explicit rerun executes the accepted SQL through the existing safety boundary.
    calls_before_rerun = model.calls
    rerun = client.post(f"/api/history/{first['run_id']}/rerun", params={"session_id": "owner"})
    assert rerun.status_code == 200, rerun.text
    assert rerun.json()["generation"]["sql"] == first["generation"]["sql"]
    assert rerun.json()["generation"]["rows"] == first["generation"]["rows"]
    assert rerun.json()["run_id"] != first["run_id"]
    assert rerun.json()["token_usage"]["input_tokens"] == 0
    assert model.calls == calls_before_rerun
    assert (
        client.post(
            f"/api/history/{first['run_id']}/rerun", params={"session_id": "other"}
        ).status_code
        == 404
    )
    client.delete("/api/history", params={"session_id": "owner"})
    assert (
        client.get(f"/api/history/{first['run_id']}", params={"session_id": "owner"}).status_code
        == 404
    )


def test_explanations_and_failures_are_saved_without_inventing_results(
    registry,
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    store = RunHistoryStore(None)
    model = CountingModel()
    client = TestClient(create_app(TextToSQLAgent(registry, model), run_history=store))
    base = {"session_id": "all-turns", "db_id": "shop", "model": "test"}
    first = client.post("/api/chat", json={**base, "message": "Count orders by customer"}).json()
    explanation = client.post(
        "/api/chat",
        json={
            **base,
            "message": "Explain the SQL",
            "parent_run_id": first["run_id"],
        },
    ).json()
    assert explanation["history_saved"]
    assert explanation["generation"] is None
    snapshot = store.get(explanation["run_id"])
    assert snapshot is not None
    assert snapshot.outcome == "EXPLANATION"
    assert snapshot.rows_preview == []
    assert snapshot.state_snapshot is not None
    assert snapshot.state_snapshot.last_sql == first["generation"]["sql"]


def test_history_bounds_the_preview_and_prunes_oldest_runs(tmp_path: Path) -> None:
    from semantic_text2sql.models import ChatRequest, ChatResponse, GenerateResponse, StrategyHints
    from semantic_text2sql.run_history import MAX_PREVIEW_ROWS, build_run_record

    response = ChatResponse(
        session_id="preview",
        operation="NEW_QUERY",
        resolved_question="List rows",
        message="Rows",
        generation=GenerateResponse(
            db_id="shop",
            question="List rows",
            provider="agentrouter",
            model="test",
            strategy=StrategyHints(mode="exact"),
            accepted=True,
            sql="SELECT 1",
            rows=[[index] for index in range(100)],
            columns=["n"],
            row_count=100,
            termination_reason="accepted",
        ),
    )
    record = build_run_record(
        {
            "response": response,
            "message": "List rows",
            "request": ChatRequest(session_id="preview", db_id="shop", message="List rows"),
        }
    )
    assert record.preview_truncated
    assert len(record.rows_preview) == MAX_PREVIEW_ROWS
    assert len(record.response_payload["generation"]["rows"]) == MAX_PREVIEW_ROWS
    assert record.row_count == 100
    store = RunHistoryStore(None)
    for index in range(503):
        store.record(record.model_copy(update={"run_id": f"run-{index}", "created_at": index}))
    assert store.get("run-0") is None
    assert store.get("run-502") is not None
    store.drop_session("preview")
    assert store.get("run-502") is None


def test_sql_diff_reports_filter_changes_and_preserved_structure() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/api/sql/diff",
        json={
            "previous_sql": "SELECT customer_id, SUM(amount) FROM orders WHERE year = 2025 "
            "GROUP BY customer_id",
            "updated_sql": "SELECT customer_id, SUM(amount) FROM orders WHERE year = 2024 "
            "AND country = 'Germany' GROUP BY customer_id",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["invariants"]["grouping"] == "unchanged"
    assert data["invariants"]["aggregations"] == "unchanged"
    assert {item["operation"] for item in data["filter_changes"]} == {
        "ADD_FILTER",
        "REPLACE_FILTER",
    }
