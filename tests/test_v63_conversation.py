"""Executable same-session tests for SQL and independent plotting orchestration."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
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
            plotting: ScriptedCompleter, sql: ScriptedSQL) -> TestClient:
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
        run_history=RunHistoryStore(None),
    ))


def _post(client: TestClient, message: str, session: str = "same") -> dict[str, Any]:
    response = client.post("/api/chat", json={
        "session_id": session, "db_id": "consumption", "provider": "agentrouter",
        "model": "scripted", "message": message,
    })
    assert response.status_code == 200, response.text
    return response.json()


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
        _tool("VISUALIZE", "change_visualization", "Plot it"),
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
    assert len(sql.calls) == 1
    assert plotting.calls == 1
    changed = _post(client,
                    "Change the plot to a horizontal bar of total consumption by currency",
                    "combined")
    assert changed["presentation"]["chart_spec"]["chart_type"] == "horizontal_bar"
    assert changed["generation"]["sql"] == combined["generation"]["sql"]
    assert len(sql.calls) == 1
    assert plotting.calls == 2


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
    assert len(sql.calls) == 1
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
            if "monthly" in question.casefold():
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
        _tool("NEW_QUERY", "ask_data", "Monthly total consumption", operation="NEW_QUERY"),
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
