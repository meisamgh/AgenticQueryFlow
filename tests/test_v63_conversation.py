"""Executable same-session tests for SQL and independent plotting orchestration."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from semantic_text2sql.agent import TextToSQLAgent
from semantic_text2sql.api import create_app
from semantic_text2sql.database import DatabaseRegistry
from semantic_text2sql.models import TokenUsage
from semantic_text2sql.run_history import RunHistoryStore


class ScriptedCompleter:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = iter(responses)
        self.calls = 0
        self.prompts: list[str] = []

    async def complete_detailed(self, model: str, prompt: str) -> tuple[str, TokenUsage]:
        self.calls += 1
        self.prompts.append(prompt)
        return json.dumps(next(self.responses)), TokenUsage(input_tokens=10, output_tokens=5)


class ScriptedSQL:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def generate(self, **kwargs: Any) -> tuple[str, int]:
        question = str(kwargs["question"])
        self.calls.append(question)
        if "currency" in question.casefold():
            return (
                "SELECT c.Currency AS currency, SUM(y.Consumption) AS total_consumption "
                "FROM yearmonth AS y JOIN customers AS c ON c.CustomerID = y.CustomerID "
                "GROUP BY c.Currency ORDER BY c.Currency",
                1,
            )
        return "SELECT SUM(Consumption) AS total_consumption FROM yearmonth", 1


def _tool(intent: str, tool: str, objective: str, **arguments: Any) -> dict[str, Any]:
    return {
        "action": "CALL_TOOL", "intent": intent, "tool": tool,
        "tool_arguments": {"objective": objective, **arguments}, "answer": None,
    }


def _answer(intent: str, answer: str) -> dict[str, Any]:
    return {"action": "ANSWER", "intent": intent, "tool": None, "answer": answer}


def _client(tmp_path: Path, monkeypatch: Any, top: ScriptedCompleter,
            plotting: ScriptedCompleter, sql: ScriptedSQL,
            history: RunHistoryStore | None = None) -> TestClient:
    directory = tmp_path / "consumption"
    directory.mkdir()
    with sqlite3.connect(directory / "consumption.sqlite") as connection:
        connection.executescript("""
            CREATE TABLE customers (CustomerID INTEGER PRIMARY KEY, Currency TEXT NOT NULL);
            CREATE TABLE yearmonth (
              CustomerID INTEGER REFERENCES customers(CustomerID),
              Date TEXT NOT NULL, Consumption REAL NOT NULL,
              PRIMARY KEY (CustomerID, Date)
            );
            INSERT INTO customers VALUES (1, 'EUR'), (2, 'CZK');
            INSERT INTO yearmonth VALUES
              (1, '202401', 10.0), (1, '202402', 20.0), (2, '202401', 5.0);
        """)
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(tmp_path))
    monkeypatch.setenv("TEXT2SQL_PROFILE_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setenv("TEXT2SQL_GLOSSARY_ROOT", str(tmp_path / "glossaries"))
    monkeypatch.setenv("TEXT2SQL_CONVERSATIONAL_AGENT_ENABLED", "true")
    monkeypatch.setenv("TEXT2SQL_PLOTTING_PROVIDER", "groq")
    return TestClient(create_app(
        TextToSQLAgent(DatabaseRegistry(tmp_path), sql),
        conversation_completers={"agentrouter": top, "groq": plotting},
        run_history=history or RunHistoryStore(None),
    ))


def _post(client: TestClient, message: str, session: str = "same") -> dict[str, Any]:
    response = client.post("/api/chat", json={
        "session_id": session, "db_id": "consumption", "provider": "agentrouter",
        "model": "scripted", "message": message,
    })
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("proposed_y", ["total_consumption", "nonexistent_measure", None])
def test_integrated_sql_chart_proposal_validates_or_selectively_falls_back(
    tmp_path: Path, monkeypatch: Any, proposed_y: str | None,
) -> None:
    initial = {
        **_tool("NEW_QUERY", "ask_data", "Total consumption by currency", operation="NEW_QUERY"),
        "finish_after_tool": True,
    }
    if proposed_y is not None:
        initial["chart_proposal"] = {
            "chart_type": "bar", "x": "currency", "y": proposed_y,
            "reason": "Compare consumption by currency.",
        }
    top = ScriptedCompleter([initial])
    plotting = ScriptedCompleter([{
        "primary_chart": {"chart_type": "bar", "x": "currency", "y": "total_consumption",
                          "reason": "Compare actual consumption values."},
        "alternative_charts": [],
    }])
    sql = ScriptedSQL()
    client = _client(tmp_path, monkeypatch, top, plotting, sql)
    response = _post(client, "Total consumption by currency and plot it", "integrated")
    assert response["plot_status"] == "CHART_READY", response
    chart = response["presentation"]["chart_spec"]
    assert chart["x"] == "currency" and chart["y"] == "total_consumption"
    assert sorted(row["total_consumption"] for row in chart["data"]) == [5.0, 30.0]
    assert response["presentation_state"]["source_result_id"] == response["result_id"]
    assert response["chart_id"]
    assert top.calls == 1
    assert len(sql.calls) == 1
    assert plotting.calls == (0 if proposed_y == "total_consumption" else 1)
    if proposed_y is not None:
        entry = next(item for item in response["call_ledger"]
                     if item["component"] == "chart_proposal_validation")
        assert entry["status"] == ("SUCCEEDED" if proposed_y == "total_consumption" else "FAILED")
        assert entry["latency_ms"] >= 0
    # A following type-only edit keeps the exact result and encodings.
    top.responses = iter([{
        **_tool("VISUALIZE", "change_visualization", "Change to a horizontal bar chart"),
        "finish_after_tool": True,
    }])
    edited = _post(client, "Change to a horizontal bar chart", "integrated")
    assert edited["result_id"] == response["result_id"]
    assert edited["chart_id"] != response["chart_id"]
    assert edited["presentation"]["chart_spec"]["y"] == "total_consumption"
    assert len(sql.calls) == 1
    assert plotting.calls == (0 if proposed_y == "total_consumption" else 1)


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_invalid_embedded_chart_is_repaired_before_any_sql_tool(
    tmp_path: Path, monkeypatch: Any, repair_succeeds: bool,
) -> None:
    bad = {
        **_tool("NEW_QUERY", "ask_data", "Total consumption by currency", operation="NEW_QUERY"),
        "finish_after_tool": True,
        "chart_proposal": {"chart_type": "bar", "x": "currency", "y": "total_consumption",
                           "javascript": "Never execute this"},
    }
    good = {**bad, "chart_proposal": {
        "chart_type": "bar", "x": "currency", "y": "total_consumption",
    }}
    top = ScriptedCompleter([bad, good if repair_succeeds else bad])
    plotting, sql = ScriptedCompleter([]), ScriptedSQL()
    response = _post(_client(tmp_path, monkeypatch, top, plotting, sql),
                     "Total consumption by currency and plot it", "invalid-proposal")
    assert top.calls == 2
    assert len(sql.calls) == (1 if repair_succeeds else 0)
    assert plotting.calls == 0
    assert (response.get("plot_status") == "CHART_READY") == repair_succeeds
    assert any(item["status"] == "FAILED" for item in response["call_ledger"])


def test_chart_only_proposal_reuses_complete_result_without_sql_or_plotting_model(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    top = ScriptedCompleter([
        {**_tool("NEW_QUERY", "ask_data", "Total consumption by currency", operation="NEW_QUERY"),
         "finish_after_tool": True},
        {**_tool("VISUALIZE", "change_visualization", "Plot the consumption by currency"),
         "finish_after_tool": True,
         "chart_proposal": {"chart_type": "bar", "x": "currency", "y": "total_consumption"}},
    ])
    plotting, sql = ScriptedCompleter([]), ScriptedSQL()
    client = _client(tmp_path, monkeypatch, top, plotting, sql)
    first = _post(client, "Total consumption by currency", "cached-design")
    plotted = _post(client, "Plot it", "cached-design")
    assert plotted["plot_status"] == "CHART_READY", plotted
    assert plotted["result_id"] == first["result_id"]
    assert plotted["presentation_state"]["source_result_id"] == first["result_id"]
    assert top.calls == 2 and len(sql.calls) == 1 and plotting.calls == 0
    assert "chart_column_profiles_advisory" in top.prompts[-1]


def test_reopening_chart_restores_complete_retained_snapshot_without_query_execution(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    class MonthlySQL(ScriptedSQL):
        async def generate(self, **kwargs: Any) -> tuple[str, int]:
            if "month" in str(kwargs["question"]).lower():
                self.calls.append(str(kwargs["question"]))
                return ("SELECT Date AS month, SUM(Consumption) AS total_consumption "
                        "FROM yearmonth GROUP BY Date ORDER BY Date", 1)
            return await super().generate(**kwargs)

    top = ScriptedCompleter([
        {**_tool("NEW_QUERY", "ask_data", "Total consumption by month", operation="NEW_QUERY"),
         "finish_after_tool": True,
         "chart_proposal": {"chart_type": "line", "x": "month", "y": "total_consumption"}},
        {**_tool("NEW_QUERY", "ask_data", "Total consumption by currency", operation="NEW_QUERY"),
         "finish_after_tool": True,
         "chart_proposal": {"chart_type": "bar", "x": "currency", "y": "total_consumption"}},
    ])
    sql, store = MonthlySQL(), RunHistoryStore(tmp_path / "history.sqlite")
    client = _client(tmp_path, monkeypatch, top, ScriptedCompleter([]), sql, history=store)
    with sqlite3.connect(tmp_path / "consumption" / "consumption.sqlite") as connection:
        connection.executemany("INSERT INTO yearmonth VALUES (1, ?, ?)", [
            (f"{2025 + index // 12}{index % 12 + 1:02d}", float(index + 1))
            for index in range(19)
        ])
    first = _post(client, "Total consumption by month and plot it", "reopen")
    assert first["generation"]["row_count"] == 21
    assert first["plot_status"] == "CHART_READY"
    saved = store.get(first["run_id"], "reopen")
    assert saved and len(saved.response_payload["generation"]["rows"]) == 20
    client.post("/api/conversations/reopen/reset")
    _post(client, "Total consumption by currency and plot it", "reopen")

    def forbidden_execute(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Opening history must not execute database SQL")

    monkeypatch.setattr(DatabaseRegistry, "execute", forbidden_execute)
    client.post("/api/conversations/reopen/reset")
    assert store.sql_memory("reopen", "consumption", "first") is None
    url = f"/api/history/{first['run_id']}/turns?session_id=reopen"
    restored = client.get(url)
    assert restored.status_code == 200
    payload = next(
        row for row in restored.json() if row["run_id"] == first["run_id"]
    )["response_payload"]
    assert payload["generation"]["rows"] == first["generation"]["rows"]
    assert payload["presentation"]["chart_spec"] == first["presentation"]["chart_spec"]
    assert payload["result_id"] == first["result_id"]
    assert payload["chart_id"] == first["chart_id"]
    assert client.get(url.replace("session_id=reopen", "session_id=another")).status_code == 404
    assert len(sql.calls) == 2 and top.calls == 2
    assert len(store.get(first["run_id"], "reopen").rows_preview) == 20
    # A restarted process must not pretend a persisted preview is complete.
    restarted = TestClient(create_app(
        TextToSQLAgent(DatabaseRegistry(tmp_path), sql), run_history=store,
    ))
    preview = restarted.get(url).json()[0]["response_payload"]
    assert len(preview["generation"]["rows"]) == 20
    assert preview["generation"]["row_count"] == 21
    assert client.delete("/api/history?session_id=reopen").status_code == 204
    assert client.get(url).status_code == 404


def test_single_tool_sql_skips_second_top_model_call(
    tmp_path: Path, monkeypatch: Any
) -> None:
    top = ScriptedCompleter([{
        **_tool("NEW_QUERY", "ask_data", "Total consumption", operation="NEW_QUERY"),
        "finish_after_tool": True,
    }])
    sql = ScriptedSQL()
    with _client(tmp_path, monkeypatch, top, ScriptedCompleter([]), sql) as client:
        response = _post(client, "Total consumption", "single-tool")
    assert response["generation"]["accepted"] is True
    assert sql.calls == ["Total consumption"]
    assert top.calls == 1
    assert "top_final_answer" not in response["timings_ms"]


def test_sql_and_chart_skip_final_top_model_call_only_after_chart(
    tmp_path: Path, monkeypatch: Any
) -> None:
    top = ScriptedCompleter([{
        **_tool("NEW_QUERY", "ask_data", "Total consumption by currency", operation="NEW_QUERY"),
        "finish_after_tool": True,
    }])
    plotting = ScriptedCompleter([{
        "primary_chart": {
            "chart_type": "bar", "x": "currency", "x_components": [],
            "y": "total_consumption", "y_measures": ["total_consumption"],
            "series": None, "reason": "Compare currency totals.",
        },
        "alternative_charts": [],
    }])
    with _client(tmp_path, monkeypatch, top, plotting, ScriptedSQL()) as client:
        response = _post(client, "Total consumption by currency and plot it", "single-chart")
    assert top.calls == 1
    assert response["generation"]["accepted"] is True
    assert response["presentation"]["chart_spec"] is not None


def test_result_pages_reuse_accepted_sql_without_client_sql_or_limit_rewrite(
    tmp_path: Path, monkeypatch: Any
) -> None:
    class OrderedSQL(ScriptedSQL):
        async def generate(self, **kwargs: Any) -> tuple[str, int]:
            self.calls.append(str(kwargs["question"]))
            return (
                "SELECT CustomerID, Date, Consumption FROM yearmonth "
                "ORDER BY CustomerID, Date", 1,
            )

    top = ScriptedCompleter([{
        **_tool("NEW_QUERY", "ask_data", "Show monthly consumption", operation="NEW_QUERY"),
        "finish_after_tool": True,
    }])
    sql = OrderedSQL()
    with _client(tmp_path, monkeypatch, top, ScriptedCompleter([]), sql) as client:
        with sqlite3.connect(tmp_path / "consumption" / "consumption.sqlite") as connection:
            connection.executemany(
                "INSERT INTO yearmonth VALUES (1, ?, ?)",
                [(f"{2025 + index // 12:04d}{index % 12 + 1:02d}", float(index))
                 for index in range(600)],
            )
        first_response = client.post("/api/chat", json={
            "session_id": "pages", "db_id": "consumption", "provider": "agentrouter",
            "model": "scripted", "message": "Show monthly consumption", "max_rows": 500,
        })
        assert first_response.status_code == 200, first_response.text
        first = first_response.json()
        assert first["generation"]["truncated"] is True
        assert len(first["generation"]["rows"]) == 500
        payload = {"session_id": "pages", "db_id": "consumption",
                   "result_id": first["result_id"], "offset": 500, "page_size": 20}
        page_response = client.post("/api/results/page", json=payload)
        assert page_response.status_code == 200, page_response.text
        page = page_response.json()
        assert len(page["rows"]) == 20
        assert page["rows"][0] != first["generation"]["rows"][0]
        assert page["total_available"] is None
        assert len(sql.calls) == 1
        wrong_result = client.post(
            "/api/results/page", json={**payload, "result_id": "wrong"}
        )
        assert wrong_result.status_code == 404
        injected = client.post(
            "/api/results/page", json={**payload, "sql": "DROP TABLE customers"}
        )
        assert injected.status_code == 422
        with sqlite3.connect(tmp_path / "consumption" / "consumption.sqlite") as connection:
            connection.execute(
                "UPDATE yearmonth SET Consumption = 999 WHERE CustomerID = 1 AND Date = '202401'"
            )
        changed = client.post("/api/results/page", json=payload)
        assert changed.status_code == 409


def test_unordered_result_cannot_be_paged_past_accepted_snapshot(
    tmp_path: Path, monkeypatch: Any
) -> None:
    class UnorderedSQL(ScriptedSQL):
        async def generate(self, **kwargs: Any) -> tuple[str, int]:
            self.calls.append(str(kwargs["question"]))
            return "SELECT CustomerID, Date, Consumption FROM yearmonth", 1

    top = ScriptedCompleter([{
        **_tool("NEW_QUERY", "ask_data", "Total consumption", operation="NEW_QUERY"),
        "finish_after_tool": True,
    }])
    with _client(tmp_path, monkeypatch, top, ScriptedCompleter([]), UnorderedSQL()) as client:
        with sqlite3.connect(tmp_path / "consumption" / "consumption.sqlite") as connection:
            connection.executemany(
                "INSERT INTO yearmonth VALUES (1, ?, ?)",
                [(f"{2025 + index // 12:04d}{index % 12 + 1:02d}", float(index))
                 for index in range(600)],
            )
        first_response = client.post("/api/chat", json={
            "session_id": "unordered", "db_id": "consumption", "provider": "agentrouter",
            "model": "scripted", "message": "Total consumption", "max_rows": 500,
        })
        assert first_response.status_code == 200
        first = first_response.json()
        response = client.post("/api/results/page", json={
            "session_id": "unordered", "db_id": "consumption",
            "result_id": first["result_id"], "offset": 500, "page_size": 20,
        })
        assert response.status_code == 409
        assert "ORDER BY" in response.json()["detail"]


def test_same_turn_new_refine_chart_uses_latest_provisional_result(
    tmp_path: Path, monkeypatch: Any
) -> None:
    top = ScriptedCompleter([
        _tool("NEW_QUERY", "ask_data", "Total consumption", operation="NEW_QUERY"),
        _tool("MODIFY_QUERY", "ask_data", "Add currency category", operation="REFINE"),
        _tool("VISUALIZE", "change_visualization", "Plot consumption by currency"),
        _answer("VISUALIZE", "Here is consumption by currency."),
    ])
    plotting = ScriptedCompleter([{
        "primary_chart": {
            "chart_type": "bar", "x": "currency", "x_components": [],
            "y": "total_consumption", "y_measures": ["total_consumption"],
            "series": None, "reason": "Compare currencies.",
        }, "alternative_charts": [],
    }])
    sql = ScriptedSQL()
    client = _client(tmp_path, monkeypatch, top, plotting, sql)
    response = _post(client, "Total consumption", "new-refine-chart")
    assert len(sql.calls) == 2, (response["message"], top.prompts)
    assert "Total consumption" in sql.calls[1]
    assert response["generation"]["columns"] == ["currency", "total_consumption"]
    assert response["presentation"]["chart_spec"]["x"] == "currency"
    assert response["presentation_state"]["source_result_id"] == response["result_id"]
    assert response["state"]["turn_count"] == 2
    assert response["timings_ms"]["sql_generation"] >= 0
    assert response["timings_ms"]["sql_validation"] >= 0
    assert response["timings_ms"]["sql_execution"] >= 0
    assert response["timings_ms"]["charting_model"] >= 0
    top_calls = [item for item in response["call_ledger"]
                 if item["component"] == "conversational_agent"]
    assert all(item["latency_ms"] is not None and item["task"] for item in top_calls)
    assert top_calls[-1]["task"] == "final_answer"
    assert not any(item["component"] == "conversation_interpretation"
                   and item["kind"] == "MODEL" for item in response["call_ledger"])


def test_same_turn_sql_refine_then_explain_uses_latest_sql(
    tmp_path: Path, monkeypatch: Any
) -> None:
    top = ScriptedCompleter([
        _tool("NEW_QUERY", "ask_data", "Total consumption", operation="NEW_QUERY"),
        _tool("MODIFY_QUERY", "ask_data", "Add currency category", operation="REFINE"),
        _tool("EXPLAIN", "explain_existing_result", "Explain the refined SQL"),
        _answer("EXPLAIN", "The refined query groups by currency."),
    ])
    sql = ScriptedSQL()
    client = _client(tmp_path, monkeypatch, top, ScriptedCompleter([]), sql)
    response = _post(client, "Total consumption", "refine-explain")
    assert len(sql.calls) == 2
    assert "GROUP BY c.Currency" in response["generation"]["sql"]
    assert "GROUP BY c.Currency" in top.prompts[-1]
    assert response["state"]["turn_count"] == 2


@pytest.mark.parametrize("tool", [
    "explain_existing_result", "analyze_result", "present_result",
])
def test_same_turn_sql_then_result_tool_reads_provisional_result(
    tmp_path: Path, monkeypatch: Any, tool: str
) -> None:
    top = ScriptedCompleter([
        _tool("NEW_QUERY", "ask_data", "Total consumption", operation="NEW_QUERY"),
        _tool("INVESTIGATE" if tool == "analyze_result" else "EXPLAIN",
              tool, "Inspect the current result"),
        _answer("INVESTIGATE" if tool == "analyze_result" else "EXPLAIN",
                "The current result has one row."),
    ])
    sql = ScriptedSQL()
    client = _client(tmp_path, monkeypatch, top, ScriptedCompleter([]), sql)
    response = _post(client, "Total consumption", f"same-{tool}")
    assert len(sql.calls) == 1
    assert response["generation"]["rows"] == [[35.0]]
    assert "35.0" in top.prompts[-1]
    assert response["message"] == "The current result has one row."
    assert response["timings_ms"]["result_explanation" if tool != "analyze_result"
                                  else "result_analysis"] >= 0


def test_existing_result_explanation_does_not_inherit_sql_timings(
    tmp_path: Path, monkeypatch: Any
) -> None:
    top = ScriptedCompleter([
        _tool("NEW_QUERY", "ask_data", "Total consumption", operation="NEW_QUERY"),
        _answer("NEW_QUERY", "Total is available."),
        _tool("EXPLAIN", "explain_existing_result", "Explain the accepted SQL"),
        _answer("EXPLAIN", "It sums consumption."),
    ])
    sql = ScriptedSQL()
    client = _client(tmp_path, monkeypatch, top, ScriptedCompleter([]), sql)
    _post(client, "Total consumption", "explain-no-sql")
    explained = _post(client, "Explain that SQL", "explain-no-sql")
    assert len(sql.calls) == 1
    assert "sql_generation" not in explained["timings_ms"]
    assert "sql_execution" not in explained["timings_ms"]
    assert explained["timings_ms"]["result_explanation"] >= 0
    assert not any(item["component"] == "sql_generation"
                   for item in explained["call_ledger"])


def test_failed_second_sql_cannot_be_reported_as_success(
    tmp_path: Path, monkeypatch: Any
) -> None:
    class FailingSecondSQL(ScriptedSQL):
        async def generate(self, **kwargs: Any) -> tuple[str, int]:
            question = str(kwargs["question"])
            if "currency" in question.casefold():
                self.calls.append(question)
                return "SELECT missing_column FROM yearmonth", 1
            return await super().generate(**kwargs)

    top = ScriptedCompleter([
        _tool("NEW_QUERY", "ask_data", "Total consumption", operation="NEW_QUERY"),
        _tool("MODIFY_QUERY", "ask_data", "Add currency category", operation="REFINE"),
        _answer("MODIFY_QUERY", "The refined analysis succeeded."),
    ])
    sql = FailingSecondSQL()
    client = _client(tmp_path, monkeypatch, top, ScriptedCompleter([]), sql)
    response = _post(client, "Total consumption", "failed-second")
    assert len(sql.calls) >= 2
    assert response["message"] != "The refined analysis succeeded."
    assert response["generation"]["rows"] == [[35.0]]


def test_historical_branch_refine_reads_selected_parent_not_latest_turn(
    tmp_path: Path, monkeypatch: Any
) -> None:
    top = ScriptedCompleter([
        _tool("NEW_QUERY", "ask_data", "Total consumption", operation="NEW_QUERY"),
        _answer("NEW_QUERY", "Total consumption is available."),
        _tool("NEW_QUERY", "ask_data", "Consumption by currency", operation="NEW_QUERY"),
        _answer("NEW_QUERY", "Currency totals are available."),
        _tool("MODIFY_QUERY", "ask_data", "Add currency category", operation="REFINE"),
        _answer("MODIFY_QUERY", "The branch is available."),
    ])
    sql = ScriptedSQL()
    client = _client(tmp_path, monkeypatch, top, ScriptedCompleter([]), sql)
    first = _post(client, "Total consumption", "history-branch")
    _post(client, "Consumption by currency", "history-branch")
    branch = client.post("/api/chat", json={
        "session_id": "history-branch", "db_id": "consumption",
        "provider": "agentrouter", "model": "scripted", "message": "Add currency category",
        "parent_run_id": first["run_id"],
    })
    assert branch.status_code == 200, branch.text
    assert branch.json()["generation"]["accepted"] is True
    assert "Total consumption" in sql.calls[-1]
    assert branch.json()["state"]["root_question"] == "Total consumption"


def test_cancelled_second_turn_does_not_commit_provisional_state(
    tmp_path: Path, monkeypatch: Any
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        class BlockingSQL(ScriptedSQL):
            async def generate(self, **kwargs: Any) -> tuple[str, int]:
                if "currency" in str(kwargs["question"]).casefold():
                    started.set()
                    await asyncio.Event().wait()
                return await super().generate(**kwargs)

        top = ScriptedCompleter([
            _tool("NEW_QUERY", "ask_data", "Total consumption", operation="NEW_QUERY"),
            _answer("NEW_QUERY", "Total is available."),
            _tool("MODIFY_QUERY", "ask_data", "Add currency category", operation="REFINE"),
        ])
        sql = BlockingSQL()
        app = _client(tmp_path, monkeypatch, top, ScriptedCompleter([]), sql).app
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            payload = {
                "session_id": "cancelled-turn", "db_id": "consumption",
                "provider": "agentrouter", "model": "scripted",
            }
            first = await client.post("/api/chat", json={**payload, "message": "Total consumption"})
            assert first.status_code == 200
            started_job = await client.post(
                "/api/chat/jobs", json={**payload, "message": "Add currency category"}
            )
            job_id = started_job.json()["job_id"]
            await asyncio.wait_for(started.wait(), timeout=3)
            cancelled = await client.delete(f"/api/chat/jobs/{job_id}")
            assert cancelled.json()["status"] == "cancelled"
            await asyncio.sleep(0)
            history = await client.get("/api/history", params={"session_id": "cancelled-turn"})
            assert len(history.json()) == 1
            assert history.json()[0]["run_id"] == first.json()["run_id"]

    asyncio.run(scenario())


def test_client_cannot_supply_provisional_turn_state(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client = _client(
        tmp_path, monkeypatch, ScriptedCompleter([]), ScriptedCompleter([]), ScriptedSQL()
    )
    response = client.post("/api/chat", json={
        "session_id": "untrusted-state", "db_id": "consumption",
        "provider": "agentrouter", "model": "scripted", "message": "Hello",
        "turn_state": {"last_sql": "SELECT secret FROM another_database"},
    })
    assert response.status_code == 422


def test_top_layer_exclusively_routes_when_legacy_bridge_is_also_enabled(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("TEXT2SQL_CONVERSATIONAL_BRIDGE_ENABLED", "true")
    top = ScriptedCompleter([_answer("DIRECT_ANSWER", "Handled by the top assistant.")])
    sql = ScriptedSQL()
    client = _client(tmp_path, monkeypatch, top, ScriptedCompleter([]), sql)
    response = _post(client, "What does retention mean?", "router-boundary")
    assert response["message"] == "Handled by the top assistant."
    assert top.calls == 1
    assert sql.calls == []


def test_database_question_cannot_be_answered_from_model_memory(
    tmp_path: Path, monkeypatch: Any
) -> None:
    top = ScriptedCompleter([
        _answer("DIRECT_ANSWER", "The total is 999."),
        _answer("NEW_QUERY", "The executed total is available."),
    ])
    sql = ScriptedSQL()
    client = _client(tmp_path, monkeypatch, top, ScriptedCompleter([]), sql)
    response = _post(client, "What is total consumption?", "grounded-answer")
    assert response["generation"]["accepted"] is True
    assert response["generation"]["rows"] == [[35.0]]
    assert response["message"] != "The total is 999."
    assert len(sql.calls) == 1


def test_result_recap_reuses_accepted_evidence_without_sql(
    tmp_path: Path, monkeypatch: Any
) -> None:
    top = ScriptedCompleter([
        _tool("NEW_QUERY", "ask_data", "Total consumption", operation="NEW_QUERY"),
        _answer("NEW_QUERY", "The total is available."),
        _tool("NEW_QUERY", "ask_data", "Show the result", operation="NEW_QUERY"),
        _answer("EXPLAIN", "The accepted total was 35."),
    ])
    sql = ScriptedSQL()
    client = _client(tmp_path, monkeypatch, top, ScriptedCompleter([]), sql)
    first = _post(client, "Total consumption", "recap")
    recap = _post(client, "Show the result", "recap")
    assert recap["result_id"] == first["result_id"]
    assert recap["generation"]["rows"] == first["generation"]["rows"]
    assert len(sql.calls) == 1


def test_concurrent_same_session_sql_turns_cannot_overwrite_accepted_state(
    tmp_path: Path, monkeypatch: Any
) -> None:
    class ConcurrentSQL(ScriptedSQL):
        def __init__(self) -> None:
            super().__init__()
            self.both_started = asyncio.Event()

        async def generate(self, **kwargs: Any) -> tuple[str, int]:
            self.calls.append(str(kwargs["question"]))
            if len(self.calls) == 2:
                self.both_started.set()
            await asyncio.wait_for(self.both_started.wait(), timeout=3)
            return "SELECT SUM(Consumption) AS total_consumption FROM yearmonth", 1

    class ConcurrentTop:
        calls = 0

        async def complete_detailed(self, model: str, prompt: str) -> tuple[str, TokenUsage]:
            self.calls += 1
            decision = (
                _tool("NEW_QUERY", "ask_data", "Total consumption", operation="NEW_QUERY")
                if self.calls <= 2 else _answer("NEW_QUERY", "The total is available.")
            )
            return json.dumps(decision), TokenUsage(input_tokens=10, output_tokens=5)

    top = ConcurrentTop()
    sql = ConcurrentSQL()
    client = _client(tmp_path, monkeypatch, top, ScriptedCompleter([]), sql)

    async def run_both() -> list[httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app), base_url="http://test"
        ) as http:
            payload = {
                "session_id": "racing-turns", "db_id": "consumption",
                "provider": "agentrouter", "model": "scripted",
                "message": "Total consumption",
            }
            return list(await asyncio.gather(
                http.post("/api/chat", json=payload), http.post("/api/chat", json=payload)
            ))

    responses = asyncio.run(run_both())
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert len(sql.calls) == 2


def test_total_then_currency_then_plot_reuses_complete_result(
    tmp_path: Path, monkeypatch: Any
) -> None:
    top = ScriptedCompleter([
        _tool("NEW_QUERY", "ask_data", "Total consumption", operation="NEW_QUERY"),
        _answer("NEW_QUERY", "Total consumption is 35."),
        _tool("MODIFY_QUERY", "ask_data", "Total consumption by currency",
              operation="REFINE", changes=[{
                  "operation": "ADD_DIMENSION", "target": "Currency",
                  "new_value": "Currency", "source": "model_interpretation",
              }]),
        _answer("MODIFY_QUERY", "Consumption is 5 CZK and 30 EUR."),
        # Even a valid but incorrect top-model NEW_QUERY decision must not
        # regenerate SQL for this bare chart follow-up.
        _tool("NEW_QUERY", "ask_data", "Plot it", operation="NEW_QUERY"),
        _answer("VISUALIZE", "Here is consumption by currency."),
    ])
    plotting = ScriptedCompleter([{
        "primary_chart": {
            "chart_type": "bar", "x": "currency", "x_components": [],
            "y": "total_consumption", "y_measures": ["total_consumption"],
            "series": None, "reason": "Compare totals by currency.",
        },
        "alternative_charts": [],
    }])
    sql = ScriptedSQL()
    client = _client(tmp_path, monkeypatch, top, plotting, sql)
    total = _post(client, "Total consumption")
    assert total["generation"]["rows"] == [[35.0]]
    grouped = _post(client, "Add currency category")
    assert grouped["generation"]["rows"] == [["CZK", 5.0], ["EUR", 30.0]]
    assert grouped["state"]["semantic_contract"]["grain"] == ["Currency"]
    chart = _post(client, "Plot it")
    assert chart["presentation"]["chart_spec"]["x"] == "currency"
    assert chart["presentation"]["chart_spec"]["y"] == "total_consumption"
    assert len(sql.calls) == 2
    assert plotting.calls == 1
    assert [entry["component"] for entry in chart["call_ledger"] if entry["kind"] == "MODEL"] == [
        "conversational_agent", "plotting_model", "conversational_agent",
    ]


def test_combined_sql_and_plot_then_change_type_without_new_sql(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from semantic_text2sql import conversational_agent as agent_module

    chart_inputs: list[tuple[bool, bool]] = []
    original_design_chart = agent_module.design_chart

    async def inspect_chart_source(**kwargs: Any) -> Any:
        source = kwargs["result"]
        chart_inputs.append((
            bool(source and source.presentation and source.presentation.chart_spec),
            bool(source and getattr(source, "chart_recommendations", None)),
        ))
        return await original_design_chart(**kwargs)

    monkeypatch.setattr(agent_module, "design_chart", inspect_chart_source)
    top = ScriptedCompleter([
        _tool("NEW_QUERY", "ask_data", "Total consumption by currency",
              operation="NEW_QUERY"),
        _tool("VISUALIZE", "change_visualization", "Plot total consumption by currency"),
        _answer("VISUALIZE", "Here is consumption by currency."),
        _tool("VISUALIZE", "change_visualization",
              "Change the plot to a horizontal bar of total consumption by currency"),
        _answer("VISUALIZE", "The same values are shown horizontally."),
    ])
    plotting = ScriptedCompleter([
        {
            "primary_chart": {
                "chart_type": kind, "x": "currency", "x_components": [],
                "y": "total_consumption", "y_measures": ["total_consumption"],
                "series": None, "reason": "Compare currency totals.",
            }, "alternative_charts": [],
        }
        for kind in ("bar", "horizontal_bar")
    ])
    sql = ScriptedSQL()
    client = _client(tmp_path, monkeypatch, top, plotting, sql)
    combined = _post(client, "Total consumption by currency and plot it", "combined")
    assert combined["generation"]["rows"] == [["CZK", 5.0], ["EUR", 30.0]]
    assert combined["presentation"]["chart_spec"]["chart_type"] == "bar"
    assert chart_inputs[0] == (False, False)
    assert len(sql.calls) == 1
    assert plotting.calls == 1
    changed = _post(client,
                    "Change the plot to a horizontal bar of total consumption by currency",
                    "combined")
    assert changed.get("presentation") is not None, (
        changed["message"], changed["plot_status"], chart_inputs, top.prompts[-1]
    )
    assert changed["presentation"]["chart_spec"] is not None
    assert changed["presentation"]["chart_spec"]["chart_type"] == "horizontal_bar"
    assert changed["generation"]["sql"] == combined["generation"]["sql"]
    assert len(sql.calls) == 1
    assert plotting.calls == 1  # Type-only follow-up executes without a plotting LLM.
    assert chart_inputs == [(False, False), (True, False)]


def test_date_consumption_then_plot_it_reuses_executed_result(
    tmp_path: Path, monkeypatch: Any
) -> None:
    class DateConsumptionSQL(ScriptedSQL):
        async def generate(self, **kwargs: Any) -> tuple[str, int]:
            self.calls.append(str(kwargs["question"]))
            return (
                "SELECT Date AS month, SUM(Consumption) AS total_consumption "
                "FROM yearmonth GROUP BY Date ORDER BY Date", 1,
            )

    top = ScriptedCompleter([
        _tool("NEW_QUERY", "ask_data", "Total consumption across date", operation="NEW_QUERY"),
        _answer("NEW_QUERY", "Here is consumption by month."),
        _tool("VISUALIZE", "change_visualization", "Plot it"),
        _tool("NEW_QUERY", "ask_data", "Plot it again", operation="NEW_QUERY"),
    ])
    plotting = ScriptedCompleter([{
        "primary_chart": {
            "chart_type": "line", "x": "month", "x_components": [],
            "y": "total_consumption", "y_measures": ["total_consumption"],
            "series": None, "reason": "Consumption over time.",
        }, "alternative_charts": [],
    }])
    sql = DateConsumptionSQL()
    client = _client(tmp_path, monkeypatch, top, plotting, sql)

    first = _post(client, "Total consumption across date", "date-plot")
    chart = _post(client, "Plot it", "date-plot")

    assert first["generation"]["columns"] == ["month", "total_consumption"]
    assert first["generation"]["rows"] == [["202401", 15.0], ["202402", 20.0]]
    assert chart["presentation"]["chart_spec"]["chart_type"] == "line"
    assert chart["presentation"]["chart_spec"]["x"] == "month"
    assert chart["result_id"] == first["result_id"]
    assert chart["presentation_state"]["source_result_id"] == first["result_id"]
    assert chart["message"] == "Here is the chart of the accepted result."
    assert len(sql.calls) == 1
    assert plotting.calls == 1


def test_total_consumption_over_date_and_plot_it_in_one_turn(
    tmp_path: Path, monkeypatch: Any
) -> None:
    class DateConsumptionSQL(ScriptedSQL):
        async def generate(self, **kwargs: Any) -> tuple[str, int]:
            self.calls.append(str(kwargs["question"]))
            return (
                "SELECT Date AS month, SUM(Consumption) AS total_consumption "
                "FROM yearmonth GROUP BY Date ORDER BY Date", 1,
            )

    top = ScriptedCompleter([{
        **_tool("NEW_QUERY", "ask_data", "Total consumption over date", operation="NEW_QUERY"),
        "finish_after_tool": True,
    }])
    plotting = ScriptedCompleter([{
        "primary_chart": {
            "chart_type": "line", "x": "month", "x_components": [],
            "y": "total_consumption", "y_measures": ["total_consumption"],
            "series": None, "reason": "Consumption over time.",
        }, "alternative_charts": [],
    }])
    sql = DateConsumptionSQL()
    with _client(tmp_path, monkeypatch, top, plotting, sql) as client:
        response = _post(client, "total consumption over date and plot it", "combined-date")
    assert response["generation"]["accepted"] is True
    assert response["presentation"]["chart_spec"] is not None, (
        top.calls, plotting.calls, top.prompts[-1][-400:],
    )
    assert len(sql.calls) == 1
    assert plotting.calls == 1


def test_plot_restores_complete_accepted_result_after_app_restart(
    tmp_path: Path, monkeypatch: Any
) -> None:
    class MonthlySQL(ScriptedSQL):
        async def generate(self, **kwargs: Any) -> tuple[str, int]:
            self.calls.append(str(kwargs["question"]))
            return (
                "SELECT Date AS month, SUM(Consumption) AS total_consumption "
                "FROM yearmonth GROUP BY Date ORDER BY Date", 1,
            )

    monkeypatch.setenv("TEXT2SQL_CONVERSATION_STORE", str(tmp_path / "conversations.sqlite"))
    history = RunHistoryStore(tmp_path / "runs.sqlite")
    first_top = ScriptedCompleter([
        _tool("NEW_QUERY", "ask_data", "Total consumption by month", operation="NEW_QUERY"),
        _answer("NEW_QUERY", "Here are the monthly totals."),
    ])
    sql = MonthlySQL()
    first_client = _client(tmp_path, monkeypatch, first_top, ScriptedCompleter([]), sql, history)
    first = _post(first_client, "Total consumption by month", "restart-plot")
    assert first["generation"]["row_count"] == 2
    second_top = ScriptedCompleter([
        _tool("VISUALIZE", "change_visualization", "Plot it"),
        _answer("VISUALIZE", "Here is the chart."),
    ])
    plotting = ScriptedCompleter([{
        "primary_chart": {
            "chart_type": "line", "x": "month", "x_components": [],
            "y": "total_consumption", "y_measures": ["total_consumption"],
            "series": None, "reason": "Monthly trend.",
        }, "alternative_charts": [],
    }])
    second_client = TestClient(create_app(
        TextToSQLAgent(DatabaseRegistry(tmp_path), sql),
        conversation_completers={"agentrouter": second_top, "groq": plotting},
        run_history=history,
    ))
    chart = _post(second_client, "Plot it", "restart-plot")
    assert chart["result_id"] == first["result_id"]
    assert chart["presentation"]["chart_spec"]["x"] == "month"
    assert len(sql.calls) == 1


def test_plot_requeries_when_restart_left_only_a_result_preview(
    tmp_path: Path, monkeypatch: Any
) -> None:
    class ManyRowsSQL(ScriptedSQL):
        async def generate(self, **kwargs: Any) -> tuple[str, int]:
            self.calls.append(str(kwargs["question"]))
            return (
                "SELECT Date AS month, SUM(Consumption) AS total_consumption "
                "FROM yearmonth GROUP BY Date ORDER BY Date", 1,
            )

    monkeypatch.setenv("TEXT2SQL_CONVERSATION_STORE", str(tmp_path / "conversations.sqlite"))
    history = RunHistoryStore(tmp_path / "runs.sqlite")
    sql = ManyRowsSQL()
    first_client = _client(
        tmp_path, monkeypatch, ScriptedCompleter([
            _tool("NEW_QUERY", "ask_data", "Show the monthly values", operation="NEW_QUERY"),
            _answer("NEW_QUERY", "Here are the values."),
        ]), ScriptedCompleter([]), sql, history,
    )
    with sqlite3.connect(tmp_path / "consumption" / "consumption.sqlite") as connection:
        connection.executemany(
            "INSERT INTO yearmonth (CustomerID, Date, Consumption) VALUES (?, ?, ?)",
            [(1, f"{year}{month:02d}", float(month))
             for year, months in ((2025, 12), (2026, 10))
             for month in range(1, months + 1)],
        )
    first = _post(first_client, "Show the monthly values", "preview-plot")
    assert first["generation"]["row_count"] == 24
    second_top = ScriptedCompleter([
        _tool("VISUALIZE", "change_visualization", "Plot it"),
        _answer("VISUALIZE", "The chart is ready."),
    ])
    plotting = ScriptedCompleter([{
        "primary_chart": {
            "chart_type": "line", "x": "month", "x_components": [],
            "y": "total_consumption", "y_measures": ["total_consumption"],
            "series": None, "reason": "Monthly trend.",
        }, "alternative_charts": [],
    }])
    second_client = TestClient(create_app(
        TextToSQLAgent(DatabaseRegistry(tmp_path), sql),
        conversation_completers={"agentrouter": second_top, "groq": plotting},
        run_history=history,
    ))
    chart = _post(second_client, "Plot it", "preview-plot")
    assert len(sql.calls) == 2
    assert chart["generation"]["row_count"] == 24
    assert chart.get("presentation") is not None, chart
    assert chart["presentation"]["chart_spec"] is not None, (
        chart["message"], chart["plot_status"], plotting.calls,
        [item["component"] for item in chart["call_ledger"]], second_top.calls,
    )
    assert chart["presentation"]["chart_spec"]["chart_type"] == "line"
    assert chart["result_id"] != first["result_id"]
    assert plotting.calls == 1


def test_chart_type_edit_overrides_wrong_new_query_route(
    tmp_path: Path, monkeypatch: Any
) -> None:
    top = ScriptedCompleter([
        _tool("NEW_QUERY", "ask_data", "Total consumption by currency",
              operation="NEW_QUERY"),
        _tool("VISUALIZE", "change_visualization", "Plot currency totals"),
        _answer("VISUALIZE", "Here is the chart."),
        _tool("NEW_QUERY", "ask_data", "Make it a horizontal bar chart",
              operation="NEW_QUERY"),
        _answer("VISUALIZE", "The same values are now horizontal."),
    ])
    plotting = ScriptedCompleter([
        {
            "primary_chart": {
                "chart_type": kind, "x": "currency", "x_components": [],
                "y": "total_consumption", "y_measures": ["total_consumption"],
                "series": None, "reason": "Compare currencies.",
            }, "alternative_charts": [],
        }
        for kind in ("bar", "horizontal_bar")
    ])
    sql = ScriptedSQL()
    client = _client(tmp_path, monkeypatch, top, plotting, sql)
    initial = _post(client, "Total consumption by currency and plot it", "chart-edit")
    changed = _post(client, "Make it a horizontal bar chart", "chart-edit")
    assert changed["presentation"]["chart_spec"]["chart_type"] == "horizontal_bar"
    assert changed["result_id"] == initial["result_id"]
    assert changed["presentation"]["chart_spec"]["x"] == "currency"
    assert changed["presentation"]["chart_spec"]["y"] == "total_consumption"
    assert len(sql.calls) == 1


def test_filter_change_requires_sql_before_plotting(
    tmp_path: Path, monkeypatch: Any
) -> None:
    class FilteredSQL(ScriptedSQL):
        async def generate(self, **kwargs: Any) -> tuple[str, int]:
            question = str(kwargs["question"])
            if "eur" in question.casefold():
                self.calls.append(question)
                return (
                    "SELECT c.Currency AS currency, SUM(y.Consumption) AS total_consumption "
                    "FROM yearmonth AS y JOIN customers AS c ON c.CustomerID = y.CustomerID "
                    "WHERE c.Currency = 'EUR' GROUP BY c.Currency", 1,
                )
            return await super().generate(**kwargs)

    top = ScriptedCompleter([
        _tool("NEW_QUERY", "ask_data", "Total consumption by currency",
              operation="NEW_QUERY"),
        _answer("NEW_QUERY", "Currency totals are available."),
        _tool("VISUALIZE", "change_visualization", "Plot it only for EUR", changes=[{
            "operation": "ADD_FILTER", "target": "Currency", "previous_value": None,
            "new_value": "EUR", "source": "model_interpretation",
        }]),
        _tool("VISUALIZE", "change_visualization", "Plot filtered currency total"),
        _answer("VISUALIZE", "Here is the filtered chart."),
    ])
    plotting = ScriptedCompleter([{
        "primary_chart": {
            "chart_type": "bar", "x": "currency", "x_components": [],
            "y": "total_consumption", "y_measures": ["total_consumption"],
            "series": None, "reason": "Filtered currency total.",
        }, "alternative_charts": [],
    }])
    sql = FilteredSQL()
    client = _client(tmp_path, monkeypatch, top, plotting, sql)
    first = _post(client, "Total consumption by currency", "filter-plot")
    followup = _post(client, "Plot it only for EUR", "filter-plot")
    assert len(sql.calls) == 2
    assert followup["result_id"] != first["result_id"]
    assert followup["generation"]["rows"] == [["EUR", 30.0]]
    assert followup["presentation"]["chart_spec"] is not None


def test_fresh_sql_result_missing_requested_dimension_cannot_be_plotted(
    tmp_path: Path, monkeypatch: Any
) -> None:
    top = ScriptedCompleter([
        _tool("NEW_QUERY", "ask_data", "Total consumption by country",
              operation="NEW_QUERY"),
        _tool("VISUALIZE", "change_visualization", "Plot total consumption by country"),
        _answer("VISUALIZE", "A country chart cannot be made from these rows."),
    ])
    plotting = ScriptedCompleter([])
    sql = ScriptedSQL()  # Deliberately returns currency, not country.
    client = _client(tmp_path, monkeypatch, top, plotting, sql)
    response = _post(client, "Total consumption by country and plot it", "fresh-gap")
    assert response["generation"]["accepted"] is True
    assert response["presentation"] is None or response["presentation"]["chart_spec"] is None
    assert response["plot_status"] == "REQUERY_REQUIRED"
    assert plotting.calls == 0


def test_missing_time_grain_is_not_invented_by_plotting(
    tmp_path: Path, monkeypatch: Any
) -> None:
    top = ScriptedCompleter([
        _tool("NEW_QUERY", "ask_data", "Total consumption by currency",
              operation="NEW_QUERY"),
        _answer("NEW_QUERY", "Here are the currency totals."),
        _tool("VISUALIZE", "change_visualization", "Plot it over months"),
        _answer("VISUALIZE", "The saved result lacks monthly detail."),
    ])
    plotting = ScriptedCompleter([])
    sql = ScriptedSQL()
    client = _client(tmp_path, monkeypatch, top, plotting, sql)
    _post(client, "Total consumption by currency", "grain")
    response = _post(client, "Plot it over months", "grain")
    assert response["presentation"] is None or response["presentation"]["chart_spec"] is None
    # Missing monthly detail requires a governed requery. The scripted SQL
    # model still returns a coarser result, which must not become a chart.
    assert len(sql.calls) == 2
    assert "Total consumption by currency" in sql.calls[1]
    assert response["plot_status"] == "REQUERY_REQUIRED"
    assert plotting.calls == 0
    assert "REQUERY_REQUIRED" in top.prompts[-1]
    assert "coarser grain" in response["message"]
    assert response["plot_status"] == "REQUERY_REQUIRED"


def test_requery_required_can_continue_through_sql_to_validated_chart(
    tmp_path: Path, monkeypatch: Any
) -> None:
    class MonthlySQL(ScriptedSQL):
        async def generate(self, **kwargs: Any) -> tuple[str, int]:
            question = str(kwargs["question"])
            if "monthly" in question.casefold() or "months" in question.casefold():
                self.calls.append(question)
                return (
                    "SELECT Date AS month, SUM(Consumption) AS total_consumption "
                    "FROM yearmonth GROUP BY Date ORDER BY Date", 1,
                )
            return await super().generate(**kwargs)

    top = ScriptedCompleter([
        _tool("NEW_QUERY", "ask_data", "Total consumption", operation="NEW_QUERY"),
        _answer("NEW_QUERY", "The total is available."),
        _tool("VISUALIZE", "change_visualization", "Plot it over months"),
        _tool("NEW_QUERY", "ask_data", "Monthly total revenue", operation="NEW_QUERY"),
        _tool("VISUALIZE", "change_visualization", "Plot monthly total consumption"),
        _answer("VISUALIZE", "Here is monthly consumption."),
    ])
    plotting = ScriptedCompleter([{
        "primary_chart": {
            "chart_type": "line", "x": "month", "x_components": [],
            "y": "total_consumption", "y_measures": ["total_consumption"],
            "series": None, "reason": "Monthly trend.",
        }, "alternative_charts": [],
    }])
    sql = MonthlySQL()
    client = _client(tmp_path, monkeypatch, top, plotting, sql)
    first = _post(client, "Total consumption", "requery-plot")
    chart = _post(client, "Plot it over months", "requery-plot")
    assert first["generation"]["columns"] == ["total_consumption"]
    assert chart["presentation"]["chart_spec"]["chart_type"] == "line"
    assert chart["generation"]["columns"] == ["month", "total_consumption"]
    assert chart["presentation_state"]["source_result_id"] == chart["result_id"]
    assert len(sql.calls) == 2
    assert "revenue" not in sql.calls[1].casefold()
    assert plotting.calls == 1


def test_requery_required_overrides_premature_answer_and_preserves_goal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    class MonthlySQL(ScriptedSQL):
        async def generate(self, **kwargs: Any) -> tuple[str, int]:
            question = str(kwargs["question"])
            if "months" in question.casefold():
                self.calls.append(question)
                return (
                    "SELECT Date AS month, SUM(Consumption) AS total_consumption "
                    "FROM yearmonth GROUP BY Date ORDER BY Date", 1,
                )
            return await super().generate(**kwargs)

    top = ScriptedCompleter([
        _tool("NEW_QUERY", "ask_data", "Total consumption", operation="NEW_QUERY"),
        _answer("NEW_QUERY", "The total is available."),
        _tool("VISUALIZE", "change_visualization", "Plot it over months"),
        _answer("VISUALIZE", "The chart is ready."),  # Premature: no monthly data yet.
        _answer("VISUALIZE", "Here is monthly consumption."),
    ])
    plotting = ScriptedCompleter([{
        "primary_chart": {
            "chart_type": "line", "x": "month", "x_components": [],
            "y": "total_consumption", "y_measures": ["total_consumption"],
            "series": None, "reason": "Monthly trend.",
        }, "alternative_charts": [],
    }])
    sql = MonthlySQL()
    client = _client(tmp_path, monkeypatch, top, plotting, sql)
    first = _post(client, "Total consumption", "bounded-requery")
    chart = _post(client, "Plot it over months", "bounded-requery")
    assert len(sql.calls) == 2
    assert "Total consumption" in sql.calls[1]
    assert chart["result_id"] != first["result_id"]
    assert chart["presentation"]["chart_spec"]["chart_type"] == "line"
    assert chart["generation"]["columns"] == ["month", "total_consumption"]
    assert plotting.calls == 1


def test_failed_current_sql_cannot_plot_stale_accepted_result(
    tmp_path: Path, monkeypatch: Any
) -> None:
    class FailSecondSQL(ScriptedSQL):
        async def generate(self, **kwargs: Any) -> tuple[str, int]:
            question = str(kwargs["question"])
            if "missing metric" in question.casefold():
                self.calls.append(question)
                return "SELECT missing_column FROM yearmonth", 1
            return await super().generate(**kwargs)

    top = ScriptedCompleter([
        _tool("NEW_QUERY", "ask_data", "Total consumption by currency",
              operation="NEW_QUERY"),
        _answer("NEW_QUERY", "Here are currency totals."),
        _tool("NEW_QUERY", "ask_data", "Plot missing metric by currency",
              operation="NEW_QUERY"),
        _tool("VISUALIZE", "change_visualization", "Plot the current result"),
        _answer("VISUALIZE", "Here is the chart."),
    ])
    plotting = ScriptedCompleter([])
    sql = FailSecondSQL()
    client = _client(tmp_path, monkeypatch, top, plotting, sql)
    accepted = _post(client, "Total consumption by currency", "stale")
    assert accepted["generation"]["accepted"] is True
    failed = _post(client, "Plot the missing metric by currency", "stale")
    assert failed["plot_status"] == "REQUERY_REQUIRED"
    assert failed["presentation"] is None or failed["presentation"]["chart_spec"] is None
    assert plotting.calls == 0
    assert "Here is the chart" not in failed["message"]
