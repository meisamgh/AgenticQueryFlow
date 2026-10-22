"""Planner decision metrics ledger behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from semantic_text2sql.analytics_planner import (
    PlannerDecisionRecord,
    PlannerMetricsLedger,
    metrics_ledger,
    reset_metrics_ledger,
)


@pytest.fixture(autouse=True)
def _fresh_ledger() -> None:
    reset_metrics_ledger()
    yield  # type: ignore[misc]
    reset_metrics_ledger()


def test_ledger_counts_by_source_and_decision() -> None:
    ledger = PlannerMetricsLedger()
    ledger.record(
        PlannerDecisionRecord(
            recorded_at="t", session_id="s", db_id="d",
            source="deterministic", operation="CHANGE_GRAIN_AND_VISUALIZE",
            reusability_decision="requery_database", requery=True, latency_ms=3,
        )
    )
    ledger.record(
        PlannerDecisionRecord(
            recorded_at="t", session_id="s", db_id="d",
            source="disabled", reusability_decision=None, requery=False,
        )
    )
    summary = ledger.summarize()
    assert summary["turns"] == 2
    assert summary["deterministic"] == 1
    assert summary["disabled"] == 1
    assert summary["requery"] == 1
    assert summary["reuse"] == 0


def test_ledger_appends_jsonl_and_caps_entries(tmp_path: Path) -> None:
    path = tmp_path / "metrics" / "planner.jsonl"
    ledger = PlannerMetricsLedger(path, max_entries=2)
    for index in range(3):
        ledger.record(
            PlannerDecisionRecord(
                recorded_at="t", session_id=f"s{index}", db_id="d", source="model"
            )
        )
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["session_id"] == "s0"
    assert len(ledger.entries()) == 2
    assert ledger.entries()[0].session_id == "s1"
    assert ledger.entries()[-1].session_id == "s2"


def test_metrics_ledger_reads_metrics_path_from_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TEXT2SQL_ANALYTICS_METRICS_PATH", str(tmp_path / "ledger.jsonl"))
    ledger = metrics_ledger()
    assert ledger._path == tmp_path / "ledger.jsonl"
    ledger.record(
        PlannerDecisionRecord(recorded_at="t", session_id="s", db_id="d", source="model")
    )
    assert (tmp_path / "ledger.jsonl").exists()
