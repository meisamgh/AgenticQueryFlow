"""SQL definitions, not numerical snapshots, are the default session memory."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from fastapi.testclient import TestClient

from semantic_text2sql.agent import TextToSQLAgent
from semantic_text2sql.api import create_app
from semantic_text2sql.models import ConversationState, SemanticContract, TokenUsage
from semantic_text2sql.run_history import RunHistoryStore, RunRecord


def record(index: int, *, accepted: bool = True, operation: str = "NEW_QUERY") -> RunRecord:
    return RunRecord(
        run_id=f"run{index}",
        result_id=f"result{index}",
        session_id="owner",
        db_id="shop",
        operation=operation,
        original_message=f"Question {index}",
        resolved_question=f"Resolved {index}",
        provider="agentrouter",
        model="test",
        accepted=accepted,
        execution_status="ACCEPTED",
        created_at=index + 1,
        execution_timestamp=index + 0.5,
        sql="SELECT country, COUNT(*) FROM customers GROUP BY country",
        rows_preview=[["Germany", 12]],
        row_count=100,
        state_snapshot=ConversationState(
            session_id="owner",
            db_id="shop",
            root_question=f"Question {index}",
            resolved_question=f"Resolved {index}",
            semantic_contract=SemanticContract(grain=["customers.country"]),
        ),
    )


@pytest.mark.parametrize("disk", [False, True])
def test_sql_memory_is_row_free_scoped_ordered_and_reset_aware(tmp_path: Path, disk: bool) -> None:
    store = RunHistoryStore(tmp_path / "history.sqlite" if disk else None)
    first = store.record(record(0))
    store.record(record(1, accepted=False))
    second = store.record(record(2))
    store.record(record(3, operation="VISUALIZE"))
    for reference in ["first", "analysis:1", "analysis:-2", first.run_id, "result0"]:
        memory = store.sql_memory("owner", "shop", reference)
        assert memory is not None and memory["run_id"] == first.run_id
        assert memory["sql"] == first.sql
        assert "rows" not in memory and "answer" not in memory
        assert not memory["verified_numerical_evidence"]
        assert memory["semantic_contract"]["grain"] == ["customers.country"]
    assert store.sql_memory("owner", "shop", "latest")["run_id"] == second.run_id  # type: ignore[index]
    assert store.sql_memory("other", "shop", "run0") is None
    assert store.sql_memory("owner", "other", "run0") is None
    assert store.sql_memory("owner", "shop", "analysis:999") is None
    assert store.sql_memory("owner", "shop", "first", parent_run_id="run2") is not None
    assert store.sql_memory("owner", "shop", "run2", parent_run_id="run0") is None
    store.mark_memory_reset("owner")
    if disk:
        store = RunHistoryStore(tmp_path / "history.sqlite")
    assert store.sql_memory("owner", "shop", "first") is None


class Planner:
    def __init__(self) -> None:
        self.decisions: list[dict[str, Any]] = []
        self.calls = 0
        self.prompts: list[str] = []

    async def complete_detailed(self, model: str, prompt: str) -> tuple[str, TokenUsage]:
        self.calls += 1
        self.prompts.append(prompt)
        return json.dumps(self.decisions.pop(0)), TokenUsage(input_tokens=10, output_tokens=5)


class SQL:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.sql = "SELECT COUNT(*) AS customer_count FROM customers"

    async def generate(self, **kwargs: Any) -> tuple[str, int, TokenUsage]:
        self.calls.append(kwargs)
        return self.sql, 1, TokenUsage(input_tokens=10, output_tokens=5)


def decision(tool: str, objective: str, intent: str = "INVESTIGATE", **args: Any) -> dict[str, Any]:
    return {
        "action": "CALL_TOOL",
        "intent": intent,
        "tool": tool,
        "tool_arguments": {"objective": objective, **args},
        "finish_after_tool": True,
    }


def app_client(
    registry: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[TestClient, Planner, SQL, RunHistoryStore]:
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    monkeypatch.setenv("TEXT2SQL_PROFILE_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setenv("TEXT2SQL_GLOSSARY_ROOT", str(tmp_path / "glossaries"))
    monkeypatch.setenv("TEXT2SQL_CONVERSATIONAL_AGENT_ENABLED", "true")
    monkeypatch.setenv("TEXT2SQL_CONTEXT_MODE", "retrieval")
    planner, sql, store = Planner(), SQL(), RunHistoryStore(tmp_path / "history.sqlite")
    client = TestClient(
        create_app(
            TextToSQLAgent(registry, sql),
            conversation_completers={"agentrouter": planner},
            run_history=store,
        )
    )
    return client, planner, sql, store


def ask(client: TestClient, question: str) -> dict[str, Any]:
    response = client.post(
        "/api/chat",
        json={
            "session_id": "owner",
            "db_id": "shop",
            "provider": "agentrouter",
            "model": "test",
            "message": question,
            "execute": True,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_historical_refresh_uses_current_data_no_generator_and_new_identity(
    registry: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client, planner, sql, store = app_client(registry, monkeypatch, tmp_path)
    planner.decisions = [
        decision("ask_data", "Count customers", "NEW_QUERY", operation="NEW_QUERY")
    ]
    first = ask(client, "Count customers")
    assert first["generation"]["rows"] == [[2]]
    with sqlite3.connect(registry.resolve("shop")) as connection:
        connection.execute("INSERT INTO customers VALUES (3, 'Ada', 'Germany')")
    planner.decisions = [
        decision("search_conversation_memory", "first", "EXPLAIN"),
        decision("refresh_analysis", first["run_id"]),
    ]
    refreshed = ask(client, "Use my first analysis with current data")
    assert refreshed["generation"]["rows"] == [[3]], refreshed
    assert len(sql.calls) == 1  # Refresh validated SQL; no second generation.
    assert planner.calls == 3  # One initial decision, then lookup and refresh.
    assert refreshed["result_id"] != first["result_id"]
    assert refreshed["parent_run_id"] == first["run_id"]
    assert "refreshed_current_data_not_historical_snapshot" in refreshed["provenance"]
    original = store.get(first["run_id"], "owner")
    assert original is not None and original.rows_preview == [[2]]
    assert store.sql_memory("owner", "shop", "first")["run_id"] == first["run_id"]  # type: ignore[index]


@pytest.mark.parametrize("integrated", [False, True])
def test_historical_refresh_and_plot_share_new_result_without_regeneration(
    registry: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    integrated: bool,
) -> None:
    monkeypatch.setenv("TEXT2SQL_PLOTTING_PROVIDER", "agentrouter")
    monkeypatch.setenv("TEXT2SQL_PLOTTING_MODEL", "test")
    client, planner, sql, _ = app_client(registry, monkeypatch, tmp_path)
    sql.sql = "SELECT country, COUNT(*) AS customer_count FROM customers GROUP BY country"
    planner.decisions = [decision("ask_data", "Customers by country", "NEW_QUERY",
                                  operation="NEW_QUERY")]
    first = ask(client, "Customers by country")
    planner.decisions = [
        decision("search_conversation_memory", "first", "EXPLAIN"),
        decision("refresh_analysis", first["run_id"]),
        {"primary_chart": {"chart_type": "bar", "x": "country",
                           "y": "customer_count", "reason": "Compare customer counts."},
         "alternative_charts": []},
    ]
    if integrated:
        planner.decisions[1]["chart_proposal"] = {
            "chart_type": "bar", "x": "country", "y": "customer_count",
            "reason": "Compare customer counts.",
        }
    plotted = ask(client, "Use my first analysis with current data and plot it")
    assert plotted["plot_status"] == "CHART_READY", plotted
    assert plotted["result_id"] != first["result_id"]
    assert plotted["presentation_state"]["source_result_id"] == plotted["result_id"]
    assert plotted["chart_id"]
    assert len(sql.calls) == 1
    assert planner.calls == (3 if integrated else 4)
    if not integrated:
        assert "Accepted analytical metadata" in planner.prompts[-1]
    assert plotted["generation"]["sql"] == first["generation"]["sql"]


def test_failed_historical_refresh_cannot_plot_unrelated_cached_result(
    registry: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("TEXT2SQL_PLOTTING_PROVIDER", "agentrouter")
    monkeypatch.setenv("TEXT2SQL_PLOTTING_MODEL", "test")
    client, planner, sql, _ = app_client(registry, monkeypatch, tmp_path)
    sql.sql = "SELECT country, COUNT(*) AS customer_count FROM customers GROUP BY country"
    planner.decisions = [decision("ask_data", "Customers by country", "NEW_QUERY",
                                  operation="NEW_QUERY")]
    ask(client, "Customers by country")
    planner.decisions = [
        decision("refresh_analysis", "missing-run"),
        decision("change_visualization", "Plot the historical analysis", "VISUALIZE"),
        {"action": "ANSWER", "intent": "DIRECT_ANSWER", "answer": "The chart is ready."},
    ]
    failed = ask(client, "Refresh the unavailable analysis and plot it")
    assert failed.get("plot_status") != "CHART_READY", failed
    assert not failed.get("chart_id")
    assert failed["message"] != "The chart is ready."
    assert len(sql.calls) == 1


def test_modified_historical_analysis_uses_selected_contract_not_latest_query(
    registry: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client, planner, sql, _ = app_client(registry, monkeypatch, tmp_path)
    planner.decisions = [
        decision("ask_data", "Count customers", "NEW_QUERY", operation="NEW_QUERY")
    ]
    first = ask(client, "Count customers")
    sql.sql = "SELECT COUNT(*) AS order_count FROM orders"
    planner.decisions = [decision("ask_data", "Count orders", "NEW_QUERY", operation="NEW_QUERY")]
    ask(client, "Count orders")
    sql.sql = "SELECT COUNT(*) AS customer_count FROM customers WHERE country = 'Germany'"
    planner.decisions = [
        decision(
            "ask_data",
            "Count customers in Germany",
            "MODIFY_QUERY",
            operation="REFINE",
            source_run_id=first["run_id"],
            changes=[
                {"operation": "ADD_FILTER", "target": "customers.country", "new_value": "Germany"}
            ],
        )
    ]
    updated = ask(client, "Use my first analysis but only Germany")
    assert updated["generation"]["accepted"], updated
    assert updated["generation"]["rows"] == [[1]]
    assert updated["parent_run_id"] == first["run_id"]
    assert "customers" in updated["state"]["root_question"].lower()


def test_schema_change_blocks_refresh_without_replacing_accepted_state(
    registry: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client, planner, sql, store = app_client(registry, monkeypatch, tmp_path)
    sql.sql = "SELECT country FROM customers"
    planner.decisions = [
        decision("ask_data", "Show customer countries", "NEW_QUERY", operation="NEW_QUERY")
    ]
    first = ask(client, "Show customer countries")
    with sqlite3.connect(registry.resolve("shop")) as connection:
        connection.execute("ALTER TABLE customers DROP COLUMN country")
    executions: list[str] = []
    original_execute = registry.execute

    def execute(*args: Any, **kwargs: Any) -> Any:
        executions.append(args[1])
        return original_execute(*args, **kwargs)

    monkeypatch.setattr(registry, "execute", execute)
    planner.decisions = [
        decision("refresh_analysis", first["run_id"]),
        {"action": "ANSWER", "intent": "DIRECT_ANSWER", "answer": "The refresh succeeded."},
    ]
    failed = ask(client, "Refresh my first analysis")
    assert "could not be rerun" in failed["message"]
    assert executions == []  # Removed field rejected before query execution.
    assert len(sql.calls) == 1
    assert store.get(first["run_id"], "owner").sql == first["generation"]["sql"]  # type: ignore[union-attr]


def test_missing_snapshot_never_refreshes_or_fabricates_old_figures(
    registry: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TEXT2SQL_HISTORY_PREVIEW_ROWS", "0")
    client, planner, sql, store = app_client(registry, monkeypatch, tmp_path)
    planner.decisions = [
        decision("ask_data", "Count customers", "NEW_QUERY", operation="NEW_QUERY")
    ]
    first = ask(client, "Count customers")
    saved = store.get(first["run_id"], "owner")
    assert saved is not None and saved.rows_preview == []
    assert saved.response_payload["generation"]["rows"] == []
    planner.decisions = [
        decision("inspect_result", f"snapshot:{first['run_id']}", "EXPLAIN"),
        {"action": "ANSWER", "intent": "DIRECT_ANSWER", "answer": "The original count was 999."},
    ]
    answer = ask(client, "What was the exact original count at that time?")
    assert "snapshot" in answer["message"].lower()
    assert "999" not in answer["message"]
    assert len(sql.calls) == 1


def test_legacy_run_id_and_indexed_result_reference_resolve_same_sql(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite"
    store = RunHistoryStore(path)
    legacy = record(0).model_copy(
        update={"result_id": None, "response_payload": {"result_id": "legacy-result"}}
    )
    store.record(legacy)
    assert store.sql_memory("owner", "shop", "legacy-result")["sql"] == legacy.sql  # type: ignore[index]
    with sqlite3.connect(path) as connection:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT record_json FROM query_runs WHERE session_id=? "
            "AND json_extract(record_json,'$.db_id')=? AND COALESCE("
            "json_extract(record_json,'$.result_id'), "
            "json_extract(record_json,'$.response_payload.result_id'))=?",
            ("owner", "shop", "legacy-result"),
        ).fetchall()
    assert any("query_runs_result_identity" in row[3] for row in plan)


def test_corrupted_historical_write_sql_is_blocked_before_execution(
    registry: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client, planner, sql, store = app_client(registry, monkeypatch, tmp_path)
    planner.decisions = [
        decision("ask_data", "Count customers", "NEW_QUERY", operation="NEW_QUERY")
    ]
    first = ask(client, "Count customers")
    original = store.get(first["run_id"], "owner")
    assert original is not None and original.state_snapshot is not None
    bad = "DELETE FROM customers"
    store.record(
        original.model_copy(
            update={
                "run_id": "corrupted",
                "sql": bad,
                "state_snapshot": original.state_snapshot.model_copy(update={"last_sql": bad}),
            }
        )
    )
    executed: list[str] = []
    monkeypatch.setattr(registry, "execute", lambda *a, **k: executed.append(a[1]))
    planner.decisions = [
        decision("refresh_analysis", "corrupted"),
        {"action": "ANSWER", "intent": "DIRECT_ANSWER", "answer": "Success."},
    ]
    response = ask(client, "Refresh the saved analysis")
    assert "could not be rerun" in response["message"]
    assert executed == [] and len(sql.calls) == 1
    with sqlite3.connect(registry.resolve("shop")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 2


def test_concurrent_refreshes_do_not_silently_overwrite_accepted_state(
    registry: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client, planner, _, _ = app_client(registry, monkeypatch, tmp_path)
    planner.decisions = [
        decision("ask_data", "Count customers", "NEW_QUERY", operation="NEW_QUERY")
    ]
    first = ask(client, "Count customers")
    barrier = Barrier(2)
    execute = registry.execute

    def blocked(*args: Any, **kwargs: Any) -> Any:
        barrier.wait(timeout=5)
        return execute(*args, **kwargs)

    monkeypatch.setattr(registry, "execute", blocked)

    def refresh() -> int:
        return client.post(
            f"/api/history/{first['run_id']}/rerun", params={"session_id": "owner"}
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: refresh(), range(2)))
    assert sorted(outcomes) == [200, 409]


def test_persisted_chart_and_rows_are_bounded_but_cached_result_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semantic_text2sql.models import (
        ChartSpec,
        ChatResponse,
        GenerateResponse,
        ResultPresentation,
        StrategyHints,
    )
    from semantic_text2sql.result_cache import RecentResult
    from semantic_text2sql.run_history import build_run_record

    rows = [[str(i), i] for i in range(100)]
    generation = GenerateResponse(
        db_id="shop",
        question="Amounts by category",
        provider="agentrouter",
        model="test",
        strategy=StrategyHints(mode="exact"),
        sql="SELECT category, amount FROM orders",
        accepted=True,
        execution_status="ACCEPTED",
        columns=["category", "amount"],
        rows=rows,
        row_count=100,
        termination_reason="accepted",
    )
    chart = ChartSpec(
        chart_type="bar",
        title="Amounts", x_label="Category", y_label="Amount",
        x="category",
        y="amount",
        data=[{"category": str(i), "amount": i} for i in range(100)],
    )
    response = ChatResponse(
        session_id="owner",
        message="Amounts",
        operation="NEW_QUERY",
        resolved_question="Amounts",
        generation=generation,
        result_id="result",
        presentation=ResultPresentation(summary="Amounts", chart_spec=chart),
    )
    saved = build_run_record({"response": response, "message": "Amounts"})
    assert len(saved.response_payload["generation"]["rows"]) == 20
    assert len(saved.response_payload["presentation"]["chart_spec"]["data"]) == 20
    assert len(RecentResult.from_response(response).rows) == 100
    monkeypatch.setenv("TEXT2SQL_HISTORY_PREVIEW_ROWS", "0")
    saved = build_run_record({"response": response, "message": "Amounts"})
    assert saved.rows_preview == []
    assert saved.response_payload["presentation"]["chart_spec"]["data"] == []
