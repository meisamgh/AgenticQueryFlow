"""Scripted SQLite integration benchmark; no live quality/latency claims.

Run this same file against the parent source with QUERYFLOW_BENCHMARK_BASELINE=1
to measure the retired stages. All figures are measured, including cache turns.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any

import pytest
from test_integrated_chart_planning import result
from test_v63_conversation import ScriptedCompleter, ScriptedSQL, _client, _tool

from semantic_text2sql.conversational_agent import ConversationalAnalyticsAgent
from semantic_text2sql.models import TokenUsage
from semantic_text2sql.visualization_recommender import profile_chart_result


class RetiredStages(ScriptedCompleter):
    async def complete_detailed(self, model: str, prompt: str) -> tuple[str, TokenUsage]:
        self.calls += 1
        await asyncio.sleep(.005)
        if "You are the Context Selector" in prompt:
            payload = {
                "selected_tables": ["customers", "yearmonth"],
                "selected_columns": {"customers": ["CustomerID", "Currency"],
                                     "yearmonth": ["CustomerID", "Date", "Consumption"]},
            }
        else:
            payload = {"primary_chart": {"chart_type": "bar", "x": "currency",
                                         "y": "total_consumption"}, "alternative_charts": []}
        return json.dumps(payload), TokenUsage(input_tokens=100, output_tokens=20)


@pytest.mark.parametrize("scenario", ["sql", "sql_chart", "cached_plot", "chart_type"])
def test_single_planner_sqlite_benchmark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str,
) -> None:
    baseline = os.environ.get("QUERYFLOW_BENCHMARK_BASELINE") == "1"
    timings: list[float] = []
    counters: list[dict[str, int]] = []
    for index in range(11):
        directory = tmp_path / str(index)
        directory.mkdir()
        decisions = []
        for _ in range(3):
            decisions.extend([
                {**_tool("NEW_QUERY", "ask_data", "Total consumption by currency",
                         operation="NEW_QUERY"), "finish_after_tool": True},
                {**_tool("VISUALIZE", "change_visualization", "Plot it"),
                 "finish_after_tool": True},
                {**_tool("VISUALIZE", "change_visualization", "Make it a horizontal bar chart"),
                 "finish_after_tool": True},
            ])
        top = ScriptedCompleter(decisions)
        auxiliary = RetiredStages([])
        sql = ScriptedSQL()
        client = _client(directory, monkeypatch, top, auxiliary, sql)

        def ask(message: str, client: Any = client) -> dict[str, Any]:
            response = client.post("/api/chat", json={
                "session_id": "benchmark", "db_id": "consumption",
                "provider": "agentrouter", "model": "scripted", "message": message,
                "context_mode": "model1", "context_provider": "groq", "context_model": "old",
            })
            assert response.status_code == 200
            return response.json()

        prior = None
        if scenario in {"cached_plot", "chart_type"}:
            prior = ask("Total consumption by currency")
        if scenario == "chart_type":
            prior = ask("Plot it")
        before = top.calls, auxiliary.calls, len(sql.calls)
        started = perf_counter()
        output = ask({"sql": "Total consumption by currency",
                      "sql_chart": "Total consumption by currency and plot it",
                      "cached_plot": "Plot it",
                      "chart_type": "Make it a horizontal bar chart"}[scenario])
        elapsed = (perf_counter() - started) * 1000
        if index:
            timings.append(elapsed)
        calls = {"top": top.calls - before[0], "retired": auxiliary.calls - before[1],
                 "sql": len(sql.calls) - before[2],
                 "database": sum(entry["kind"] == "DATABASE"
                                 for entry in output["call_ledger"])}
        if index:
            counters.append(calls)
        assert output["generation"]["accepted"]
        assert sorted(row[-1] for row in output["generation"]["rows"]) == [5.0, 30.0]
        if prior:
            assert output["result_id"] == prior["result_id"]
            assert calls["sql"] == 0
        if scenario != "sql":
            assert output["presentation"]["chart_spec"]["y"] == "total_consumption"
            assert output["plot_status"] == "CHART_READY"
        assert calls["top"] == 1
        assert calls["database"] == (0 if prior else 2)  # Schema inspection + execution.
        assert calls["retired"] == (
            {"sql": 1, "sql_chart": 2, "cached_plot": 1, "chart_type": 0}[scenario]
            if baseline else 0
        )
        client.close()
    print(json.dumps({"scenario": scenario, "baseline": baseline,
                      "p50_ms": round(median(timings), 3),
                      "p95_ms": round(sorted(timings)[-1], 3), "calls": counters[0],
                      "total_llm_calls": sum(counters[0][key] for key in ("top", "retired", "sql")),
                      "auxiliary_input_tokens": counters[0]["retired"] * 100,
                      "auxiliary_output_tokens": counters[0]["retired"] * 20}))


def test_routine_result_context_has_profiles_not_row_previews() -> None:
    response = result()
    facts = ConversationalAnalyticsAgent._result_facts(response)
    assert facts["result_id"] == response.result_id
    assert "rows_preview" not in facts
    assert "revenue" in facts["column_profiles"]
    assert facts["truncated"] is False
    cached = ConversationalAnalyticsAgent._cached_facts(response)
    assert cached["result_id"] == response.result_id
    assert "rows_preview" not in cached and "accepted_sql" not in cached


def test_compact_profiles_expose_measure_ranges_not_raw_identifiers() -> None:
    profile, sample = profile_chart_result(
        ["customer_id", "revenue", "email"],
        [[987654321, 100.0, "private@example.test"], [987654322, None, "other@example.test"]],
        sample_rows=0, include_ranges=True,
    )
    assert sample == "[]"
    assert "range=[100.0, 100.0]" in profile
    assert "987654321" not in profile and "private@example.test" not in profile
    assert "types=['float']" in profile and "nulls=1" in profile
