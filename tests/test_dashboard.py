from __future__ import annotations

import json
from dataclasses import replace
from statistics import median
from time import perf_counter
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from semantic_text2sql.agent import TextToSQLAgent
from semantic_text2sql.api import create_app
from semantic_text2sql.dashboard_service import (
    DashboardError,
    DashboardRequest,
    DashboardSpec,
    build_dashboard,
)
from semantic_text2sql.models import (
    ChartRecommendations,
    ChatResponse,
    GenerateResponse,
    RecommendedChart,
    ResultPresentation,
    SemanticContract,
    StrategyHints,
)
from semantic_text2sql.result_cache import RecentResult
from semantic_text2sql.visualization import VisualizationIntent, plan_chart


def snapshot() -> RecentResult:
    columns = ["month", "country", "revenue"]
    rows = [
        ["202601", "DE", 100.0],
        ["202601", "FR", 200.0],
        ["202602", "DE", 150.0],
        ["202602", "FR", 250.0],
    ]
    spec = plan_chart(
        VisualizationIntent(
            requested=True, chart_type="multi_line", x="month", y="revenue", series="country"
        ),
        question="",
        columns=columns,
        rows=rows,
        truncated=False,
    )
    response = ChatResponse(
        session_id="owner",
        run_id="run",
        result_id="result",
        chart_id="chart",
        operation="NEW_QUERY",
        resolved_question="Monthly revenue by country",
        message="Done",
        generation=GenerateResponse(
            db_id="sales",
            question="Monthly revenue by country",
            provider="agentrouter",
            model="test",
            sql="SELECT month, country, SUM(amount) AS revenue FROM sales "
            "WHERE status = 'complete' GROUP BY month, country",
            strategy=StrategyHints(mode="exact"),
            accepted=True,
            execution_status="ACCEPTED",
            termination_reason="accepted",
            rows=rows,
            columns=columns,
            row_count=len(rows),
            semantic_contract=SemanticContract(comparison_periods=["202601", "202602"]),
            model_context={
                "tables": {"sales": {"grain": "one row per sale_id", "primary_key": ["sale_id"]}}
            },
        ),
        presentation=ResultPresentation(summary="Done", chart_spec=spec),
    )
    return RecentResult.from_response(response)


def build(source: RecentResult, include_data: bool = True) -> DashboardSpec:
    return build_dashboard(
        DashboardRequest(
            session_id="owner", db_id="sales", result_id="result", include_data=include_data
        ),
        lambda session, db, identity: source,
    )


def test_single_result_kpis_charts_filters_and_layout_are_deterministic() -> None:
    source = snapshot()
    before = source.generation.model_dump_json()
    dashboard = build(source)
    assert dashboard == build(source)
    assert dashboard.section_order == ("filters", "kpis", "charts", "data")
    assert dashboard.applied_constraints == ["status = 'complete'"]
    values = {k.calculation: k for k in dashboard.kpis}
    assert values["TOTAL"].value == 700
    assert values["AVERAGE"].value == 175
    assert values["COUNT"].value == 4
    assert "not distinct customers" in values["COUNT"].definition
    assert values["PERIOD_GROWTH"].value == pytest.approx(100 / 3)
    assert values["PERIOD_GROWTH"].baseline == 300
    assert values["PERIOD_GROWTH"].current == 400
    assert 1 <= len(dashboard.widgets) <= 4
    assert all(widget.result_id == "result" for widget in dashboard.widgets)
    assert all(widget.chart_spec.source_row_count == 4 for widget in dashboard.widgets)
    assert dashboard.data and dashboard.data.rows == source.rows
    assert source.generation.model_dump_json() == before
    assert build(source, include_data=False).data is None


@pytest.mark.parametrize(
    "change",
    [
        {"truncated": True},
        {"row_count": 5},
        {"result_id": "other"},
        {"db_id": "other"},
        {"execution_status": "SAFETY_VALIDATED"},
        {"columns": ["month", "revenue", "revenue"]},
    ],
)
def test_invalid_incomplete_or_wrong_identity_rejected(change: dict[str, Any]) -> None:
    with pytest.raises(DashboardError):
        build(replace(snapshot(), **change))


def test_missing_result_rejected() -> None:
    with pytest.raises(DashboardError, match="unavailable"):
        build_dashboard(
            DashboardRequest(session_id="owner", db_id="sales", result_id="missing"),
            lambda *_: None,
        )


@pytest.mark.parametrize(
    "expression",
    [
        "AVG(amount)",
        "COUNT(DISTINCT customer_id)",
        "SUM(amount) / COUNT(*)",
        "SUM(DISTINCT amount)",
    ],
)
def test_average_of_averages_distinct_counts_and_ratios_are_not_reaggregated(
    expression: str,
) -> None:
    source = replace(
        snapshot(),
        sql=f"SELECT month, country, {expression} AS revenue FROM sales GROUP BY month, country",
    )
    dashboard = build(source)
    assert not any(k.calculation in {"TOTAL", "AVERAGE", "PERIOD_GROWTH"} for k in dashboard.kpis)
    assert not any(
        widget.chart_spec.chart_type
        in {"pie", "donut", "stacked_bar", "percent_stacked_bar", "treemap", "stacked_area"}
        for widget in dashboard.widgets
    )


def test_unknown_source_grain_joins_and_limited_populations_do_not_prove_total() -> None:
    for source in [
        replace(snapshot(), verified_context={}),
        replace(snapshot(), sql=snapshot().sql + " LIMIT 3"),
        replace(
            snapshot(),
            sql="SELECT month, country, SUM(amount) AS revenue "
            "FROM sales JOIN users ON sales.customer_id = users.id "
            "GROUP BY month, country",
        ),
    ]:
        assert not any(k.calculation == "TOTAL" for k in build(source).kpis)


def test_growth_requires_explicit_matching_nonzero_comparison() -> None:
    for source in [
        replace(snapshot(), semantic_contract=None),
        replace(
            snapshot(), semantic_contract=SemanticContract(comparison_periods=["202601", "202601"])
        ),
        replace(
            snapshot(),
            rows=[
                ["202601", "DE", 0.0],
                ["202601", "FR", 0.0],
                ["202602", "DE", 150.0],
                ["202602", "FR", 250.0],
            ],
        ),
        replace(
            snapshot(),
            rows=[
                ["202601", "DE", 100.0],
                ["202601", "GB", 200.0],
                ["202602", "DE", 150.0],
                ["202602", "FR", 250.0],
            ],
        ),
    ]:
        assert not any(k.calculation == "PERIOD_GROWTH" for k in build(source).kpis)
    # A dashboard's own validated temporal chart can establish the time format
    # when the original accepted result had no visualization attached.
    assert any(
        k.calculation == "PERIOD_GROWTH" for k in build(replace(snapshot(), chart_spec=None)).kpis
    )


def test_up_to_four_complementary_charts_and_invalid_candidates_are_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semantic_text2sql import dashboard_service

    candidates = [
        RecommendedChart(
            chart_type=kind, x="month", y="revenue", series="country", title="Revenue", score=1
        )
        for kind in ("multi_line", "grouped_bar", "stacked_bar", "heatmap", "area")
    ]
    monkeypatch.setattr(
        dashboard_service,
        "recommend_charts",
        lambda *a, **k: ChartRecommendations(
            primary_chart=candidates[0], alternative_charts=candidates[1:]
        ),
    )
    dashboard = build(snapshot())
    assert len(dashboard.widgets) == 4
    assert {w.chart_spec.chart_type for w in dashboard.widgets} == {
        "multi_line",
        "grouped_bar",
        "stacked_bar",
        "heatmap",
    }
    bad = candidates[0].model_copy(update={"y": "fabricated_column"})
    monkeypatch.setattr(
        dashboard_service,
        "recommend_charts",
        lambda *a, **k: ChartRecommendations(primary_chart=bad),
    )
    assert build(snapshot()).widgets == []


def test_empty_and_scalar_results_are_faithful() -> None:
    empty = build(replace(snapshot(), rows=[], row_count=0))
    assert empty.kpis == [] and empty.widgets == []
    assert empty.data and empty.data.rows == []
    assert any("no rows" in message for message in empty.notices)
    scalar = build(
        replace(
            snapshot(),
            rows=[[12]],
            columns=["customer_count"],
            row_count=1,
            sql="SELECT COUNT(DISTINCT customer_id) AS customer_count FROM sales",
        )
    )
    assert scalar.kpis[0].calculation == "DIRECT" and scalar.kpis[0].value == 12
    assert len(scalar.kpis) == 1


def test_dashboard_model_rejects_cross_result_widget() -> None:
    payload = build(snapshot()).model_dump()
    payload["widgets"][0]["result_id"] = "other"
    with pytest.raises(ValidationError):
        DashboardSpec.model_validate(payload)


def test_endpoint_reuses_accepted_result_without_schema_db_or_model_calls(
    registry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    monkeypatch.setenv("TEXT2SQL_CONVERSATIONAL_AGENT_ENABLED", "false")
    monkeypatch.setenv("TEXT2SQL_CONTEXT_MODE", "retrieval")

    class SQL:
        calls = 0

        async def generate(self, **kwargs: Any) -> tuple[str, int]:
            self.calls += 1
            return "SELECT status, SUM(amount) AS total_amount FROM orders GROUP BY status", 1

    model = SQL()
    client = TestClient(create_app(TextToSQLAgent(registry, model)))
    response = client.post(
        "/api/chat",
        json={
            "session_id": "owner",
            "db_id": "shop",
            "provider": "agentrouter",
            "model": "test",
            "message": "Sum amount by status",
        },
    ).json()
    assert response["generation"]["accepted"], response

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Dashboard must not inspect or execute database SQL")

    monkeypatch.setattr(registry, "execute", forbidden)
    monkeypatch.setattr(registry, "inspect", forbidden)
    monkeypatch.setattr(model, "generate", forbidden)
    request = {
        "session_id": "owner",
        "db_id": "shop",
        "result_id": response["result_id"],
        "include_data": True,
    }
    dashboard = client.post("/api/dashboards", json=request)
    assert dashboard.status_code == 200, dashboard.text
    spec = DashboardSpec.model_validate(dashboard.json())
    assert dashboard.headers["cache-control"] == "no-store"
    assert spec.data and spec.data.rows == response["generation"]["rows"]
    assert spec.result_id == response["result_id"] and model.calls == 1
    assert next(k.value for k in spec.kpis if k.calculation == "TOTAL") == 170
    page = client.post(
        "/api/results/page",
        json={
            "session_id": "owner",
            "db_id": "shop",
            "result_id": response["result_id"],
            "offset": 0,
            "page_size": 20,
        },
    )
    assert page.status_code == 200
    assert page.json()["rows"] == response["generation"]["rows"]
    assert (
        client.post("/api/dashboards", json={**request, "session_id": "other"}).status_code == 404
    )
    assert client.post("/api/dashboards", json={**request, "db_id": "unknown"}).status_code == 404
    assert (
        client.post("/api/dashboards", json={**request, "result_id": "missing"}).status_code == 404
    )
    assert (
        client.post(
            "/api/dashboards", json={**request, "result_id": "SELECT * FROM orders"}
        ).status_code
        == 422
    )


def test_dashboard_build_microbenchmark() -> None:
    source = snapshot()
    durations = []
    for _ in range(30):
        started = perf_counter()
        dashboard = build(source)
        durations.append((perf_counter() - started) * 1000)
    print(
        f"dashboard 4-row fixture: p50={median(durations):.3f}ms "
        f"p95={sorted(durations)[28]:.3f}ms bytes={len(dashboard.model_dump_json().encode())}"
    )
    assert json.loads(dashboard.model_dump_json())["result_id"] == "result"


def test_dashboard_500_row_microbenchmark_and_row_integrity() -> None:
    source = replace(
        snapshot(),
        rows=[
            [
                f"{2020 + month // 12}{month % 12 + 1:02d}",
                f"Country-{country}",
                float(month + country),
            ]
            for month in range(100)
            for country in range(5)
        ],
        row_count=500,
    )
    durations = []
    for _ in range(30):
        started = perf_counter()
        dashboard = build(source)
        durations.append((perf_counter() - started) * 1000)
    assert dashboard.data and dashboard.data.rows == source.rows
    assert all(w.chart_spec.source_row_count == 500 for w in dashboard.widgets)
    print(
        f"dashboard 500-row fixture: p50={median(durations):.3f}ms "
        f"p95={sorted(durations)[28]:.3f}ms bytes={len(dashboard.model_dump_json().encode())}"
    )
