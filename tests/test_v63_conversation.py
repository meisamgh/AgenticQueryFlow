"""Executable same-session tests for SQL and independent plotting orchestration."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

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
        _tool("VISUALIZE", "change_visualization", "Change to a horizontal bar"),
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
    changed = _post(client, "Change the chart to a horizontal bar", "combined")
    assert changed["presentation"]["chart_spec"]["chart_type"] == "horizontal_bar"
    assert changed["generation"]["sql"] == combined["generation"]["sql"]
    assert len(sql.calls) == 1
    assert plotting.calls == 2


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
