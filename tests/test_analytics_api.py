"""End-to-end chat tests for the analytics planner and reusability gate."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi.testclient import TestClient

from semantic_text2sql.agent import TextToSQLAgent
from semantic_text2sql.api import create_app
from semantic_text2sql.database import DatabaseRegistry
from semantic_text2sql.models import TokenUsage
from semantic_text2sql.run_history import RunHistoryStore


class SessionTurnModel:
    """Deterministic conversation interpreter for stateful follow-ups."""

    async def complete_detailed(self, model: str, prompt: str):  # type: ignore[no-untyped-def]
        import json

        return (
            json.dumps(
                {
                    "operation": "CHANGE_GRAIN",
                    "depends_on_previous": True,
                    "resolved_instruction": prompt.rsplit("Current message: ", 1)[1],
                    "correction_type": None,
                    "confidence": 0.95,
                },
            ),
            TokenUsage(),
        )


class KeywordSQLModel:
    """Deterministic stand-in that switches SQL on the requested grain."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, **kwargs: Any) -> tuple[str, int]:
        self.calls += 1
        question = str(kwargs.get("question", "")).casefold()
        if "yearly" in question or "per year" in question:
            return (
                "SELECT substr(month, 1, 4) AS year, SUM(consumption) AS total_consumption "
                "FROM meters GROUP BY year",
                3,
            )
        if "monthly" in question or "month" in question:
            return (
                "SELECT month, SUM(consumption) AS total_consumption "
                "FROM meters GROUP BY month ORDER BY month",
                3,
            )
        return "SELECT SUM(consumption) AS total_consumption FROM meters", 2


def _root(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    directory = tmp_path / "utility"
    directory.mkdir()
    with sqlite3.connect(directory / "utility.sqlite") as connection:
        connection.executescript(
            """
            CREATE TABLE meters (
              meter_id INTEGER PRIMARY KEY,
              month TEXT NOT NULL,
              consumption REAL NOT NULL
            );
            INSERT INTO meters VALUES
              (1, '202501', 100), (2, '202502', 120),
              (3, '202503', 140), (4, '202504', 160);
            """
        )
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(tmp_path))
    monkeypatch.setenv("TEXT2SQL_PROFILE_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setenv("TEXT2SQL_GLOSSARY_ROOT", str(tmp_path / "glossaries"))


def _client(tmp_path, monkeypatch) -> tuple[TestClient, KeywordSQLModel]:  # type: ignore[no-untyped-def]
    _root(tmp_path, monkeypatch)
    model = KeywordSQLModel()
    store = RunHistoryStore(None)
    client = TestClient(
        create_app(
            TextToSQLAgent(DatabaseRegistry(tmp_path), model),
            conversation_completers={"agentrouter": SessionTurnModel()},
            run_history=store,
        )
    )
    return client, model


def test_scenario_a_scalar_total_per_month_routes_through_validated_new_sql(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    client, model = _client(tmp_path, monkeypatch)
    base = {"session_id": "analytic-a", "db_id": "utility",
            "provider": "agentrouter", "model": "test-model"}

    scalar = client.post(
        "/api/chat", json={**base, "message": "What is the total consumption in 2025?"}
    )
    assert scalar.status_code == 200
    first = scalar.json()
    assert first["generation"]["accepted"] is True
    assert first["generation"]["row_count"] == 1
    scalar_calls = model.calls

    followup = client.post("/api/chat", json={**base, "message": "Plot it per month."})
    assert followup.status_code == 200
    answer = followup.json()
    # The gate must route this through the SQL pipeline instead of clarifying
    # or inventing monthly points from the scalar total.
    assert answer.get("clarification_required") in (False, None)
    assert answer["operation"] == "CHANGE_GRAIN"
    assert answer["generation"]["accepted"] is True
    monthly_sql = answer["generation"]["sql"]
    assert "GROUP BY" in monthly_sql and "month" in monthly_sql
    assert answer["generation"]["row_count"] == 4
    assert answer["presentation"] is not None
    assert answer["presentation"]["chart_spec"] is not None
    assert answer["presentation"]["chart_spec"]["chart_type"] == "line"
    assert model.calls == scalar_calls + 1


def test_scenario_b_complete_monthly_result_reused_without_new_sql(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    client, model = _client(tmp_path, monkeypatch)
    base = {"session_id": "analytic-b", "db_id": "utility",
            "provider": "agentrouter", "model": "test-model"}

    monthly = client.post(
        "/api/chat", json={**base, "message": "Show monthly consumption."}
    )
    assert monthly.status_code == 200
    first = monthly.json()
    assert first["generation"]["accepted"] is True
    assert first["generation"]["row_count"] == 4
    calls_after_first = model.calls

    plotted = client.post("/api/chat", json={**base, "message": "Plot it."})
    assert plotted.status_code == 200
    chart = plotted.json()
    assert chart["operation"] == "VISUALIZE"
    assert chart["presentation"]["chart_spec"]["chart_type"] == "line"
    assert chart["generation"]["sql"] == first["generation"]["sql"]
    # No new SQL run, no new model generation call.
    assert model.calls == calls_after_first


def test_coarsening_grain_change_on_stored_rows_requeries_validated_sql(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    client, model = _client(tmp_path, monkeypatch)
    base = {"session_id": "analytic-c", "db_id": "utility",
            "provider": "agentrouter", "model": "test-model"}

    client.post("/api/chat", json={**base, "message": "Show monthly consumption."})
    calls_before = model.calls
    followup = client.post("/api/chat", json={**base, "message": "Now plot it yearly."})
    assert followup.status_code == 200
    answer = followup.json()
    # The monthly aggregate cannot be regrouped client-side by this pipeline's
    # semantics; a validated yearly query preserves the analytical meaning.
    assert answer.get("clarification_required") in (False, None)
    assert answer["generation"]["accepted"] is True
    assert answer["generation"]["sql"] is not None
    assert model.calls == calls_before + 1
