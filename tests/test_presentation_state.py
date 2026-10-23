"""Acceptance tests for the versioned, backend-driven presentation state.

Covered scenarios:
1. Chart persistence across analytical corrections ("Only Germany").
2. A new independent question stays in the chat without inheriting the chart.
3. Unsupported/truncated charts degrade truthfully without SQL regeneration.
4. Saved history restores the typed presentation state without rerunning SQL.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from semantic_text2sql.agent import TextToSQLAgent
from semantic_text2sql.api import create_app
from semantic_text2sql.run_history import RunHistoryStore
from semantic_text2sql.visualization import parse_intent


class FixedSQLModel:
    """Always returns the same validated SQL; conversation logic drives variation."""

    def __init__(self, sql: str) -> None:
        self.sql = sql
        self.calls = 0

    async def generate(self, **kwargs: Any) -> tuple[str, int]:
        self.calls += 1
        return self.sql, 1


def _client(monkeypatch, registry, tmp_path, *, store: RunHistoryStore | None = None, sql: str):
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    monkeypatch.setenv("TEXT2SQL_PROFILE_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setenv("TEXT2SQL_GLOSSARY_ROOT", str(tmp_path / "glossaries"))
    model = FixedSQLModel(sql)
    agent = TextToSQLAgent(registry, model)
    return TestClient(create_app(agent, run_history=store or RunHistoryStore(None))), model


BASE = {
    "session_id": "pres-state",
    "db_id": "shop",
    "provider": "agentrouter",
    "model": "test-model",
}
COUNTS_SQL = "SELECT country, COUNT(*) AS customer_count FROM customers GROUP BY country"


def test_analytical_correction_inherits_chart_mode(monkeypatch, registry, tmp_path) -> None:  # type: ignore[no-untyped-def]
    client, model = _client(
        monkeypatch, registry, tmp_path,
        sql=COUNTS_SQL,
    )

    plot = client.post("/api/chat", json={
        **BASE, "message": "Plot customer counts by country as a bar chart.",
    })
    assert plot.status_code == 200
    first = plot.json()
    assert first["presentation"]["chart_spec"]["chart_type"] == "bar"
    assert first["presentation_state"]["mode"] == "chart"
    assert first["presentation_state"]["chart_type"] == "bar"
    assert first["presentation_state"]["source_run_id"] == first["run_id"]
    first_calls = model.calls

    correction = client.post("/api/chat", json={**BASE, "message": "Only Germany."})
    assert correction.status_code == 200
    second = correction.json()
    assert second["operation"] in {"ADD_FILTER", "REFINE"}
    # The correction stays chart-first with new validated SQL, no client regex.
    assert second["presentation_state"]["mode"] == "chart"
    assert second["presentation"]["chart_spec"]["chart_type"] == "bar"
    assert second["generation"]["accepted"] is True
    assert second["presentation_state"]["source_run_id"] == second["run_id"]
    assert model.calls > first_calls or second["generation"]["sql"] != first["generation"]["sql"]


def test_new_independent_question_stays_in_chat_without_inheriting_chart(
    monkeypatch, registry, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    client, _ = _client(monkeypatch, registry, tmp_path, sql=COUNTS_SQL)

    plotted = client.post("/api/chat", json={
        **BASE, "message": "Plot customer counts by country as a bar chart.",
    })
    assert plotted.json()["presentation_state"]["mode"] == "chart"

    independent = client.post("/api/chat", json={
        **BASE, "message": "List each country with its customer count.",
    })
    assert independent.status_code == 200
    fresh = independent.json()
    assert fresh["operation"] == "NEW_QUERY"
    assert fresh["clarification_required"] is False
    assert fresh["generation"]["accepted"] is True
    assert fresh["presentation"]["chart_spec"] is None
    assert fresh["state"]["root_question"] == "List each country with its customer count."

    follow_up = client.post("/api/chat", json={**BASE, "message": "Only Germany."}).json()
    assert follow_up["operation"] in {"ADD_FILTER", "REFINE"}
    assert follow_up["state"]["root_question"] == fresh["state"]["root_question"]


def test_truncated_result_never_produces_a_chart(monkeypatch, registry, tmp_path) -> None:  # type: ignore[no-untyped-def]
    client, _ = _client(monkeypatch, registry, tmp_path, sql=COUNTS_SQL)

    incomplete = client.post(
        "/api/chat",
        json={
            **BASE,
            "session_id": "pres-truncated",
            "message": "Plot customer counts by country.",
            "max_rows": 1,
        },
    )
    assert incomplete.status_code == 200
    body = incomplete.json()
    assert body["generation"]["truncated"] is True
    assert body["presentation"]["chart_spec"] is None
    assert body["presentation_state"]["mode"] == "sql"


def test_history_restores_presentation_state_without_rerunning(
    monkeypatch, registry, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    store: RunHistoryStore = RunHistoryStore(None)
    client, model = _client(monkeypatch, registry, tmp_path, store=store, sql=COUNTS_SQL)

    plotted = client.post("/api/chat", json={
        **BASE, "message": "Plot customer counts by country as a bar chart.",
    })
    run_id = plotted.json()["run_id"]
    calls_after_plot = model.calls

    saved = store.get(run_id, "pres-state")
    assert saved is not None
    assert saved.presentation_state is not None
    assert saved.presentation_state.mode == "chart"
    assert saved.response_payload["presentation_state"]["mode"] == "chart"

    turns = client.get(f"/api/history/{run_id}/turns?session_id=pres-state")
    assert turns.status_code == 200
    records = turns.json()
    assert records
    record = records[-1]
    assert record["presentation_state"]["mode"] == "chart"
    assert record["response_payload"]["presentation"]["chart_spec"] is not None
    # Reopening history never executes SQL.
    assert model.calls == calls_after_plot


def test_visualization_intent_extraction_is_deterministic() -> None:
    monthly = parse_intent("Stacked bar chart of monthly revenue by country.")
    assert monthly.requested is True
    assert monthly.chart_type == "stacked_bar"
    assert monthly.series == "country"

    scatter = parse_intent("Scatter plot of spending against transaction count.")
    assert scatter.chart_type == "scatter"
    assert scatter.y == "transaction count"

    plain = parse_intent("Plot revenue by month.")
    assert plain.requested is True
    assert plain.x == "month"

    horizontal = parse_intent("Make the existing chart horizontal.", has_previous=True)
    assert horizontal.chart_type == "horizontal_bar"

    vertical = parse_intent("Make it vertical instead.", has_previous=True)
    assert vertical.chart_type == "bar"

    ordinary = parse_intent("Show the top customers by spending.")
    assert ordinary.requested is False
