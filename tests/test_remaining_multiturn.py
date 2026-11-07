"""Real SQLite evidence for previously unmapped V6.4 comparison/grain scenarios."""
import sqlite3
from typing import Any

import pytest
from test_v63_conversation import ScriptedCompleter, ScriptedSQL, _client, _post, _tool

from semantic_text2sql.validator import normalize_sql


@pytest.fixture(autouse=True)
def stable_generated_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    # These tests assert generator/state SQL identity, not timing-based optimizer
    # selection. Equivalent predicate reordering may otherwise win nondeterministically.
    # Optimizer correctness and selection remain covered in test_sqlglot_optimization.py.
    monkeypatch.setattr("semantic_text2sql.agent._sqlglot_optimization_candidate",
                        lambda *args, **kwargs: None)


class SQLSequence(ScriptedSQL):
    def __init__(self, queries: list[str]) -> None:
        super().__init__()
        self.statements = list(queries)
        self.queries = iter(queries)

    async def generate(self, **kwargs: Any) -> tuple[str, int]:
        self.calls.append(str(kwargs["question"]))
        return next(self.queries), 1


def decision(objective: str, *, first: bool = False, changes: Any = None) -> dict[str, Any]:
    return {**_tool("NEW_QUERY" if first else "MODIFY_QUERY", "ask_data", objective,
                    operation="NEW_QUERY" if first else "REFINE", changes=changes or []),
            "finish_after_tool": True}


def test_monthly_to_yearly_uses_new_sql_and_preserves_accepted_metric(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    top = ScriptedCompleter([
        decision("Total consumption by month", first=True),
        decision("Same consumption grouped by year", changes=[{
            "operation": "CHANGE_GRAIN", "target": "grain", "new_value": "year"}]),
    ])
    sql = SQLSequence([
        "SELECT Date AS month, SUM(Consumption) AS total FROM yearmonth GROUP BY Date",
        "SELECT SUBSTR(Date,1,4) AS year, SUM(Consumption) AS total "
        "FROM yearmonth GROUP BY SUBSTR(Date,1,4)",
    ])
    client = _client(tmp_path, monkeypatch, top, ScriptedCompleter([]), sql)
    first = _post(client, "Total consumption by month", "grain-transition")
    yearly = _post(client, "Group it by year instead", "grain-transition")
    assert first["generation"]["accepted"] and yearly["generation"]["accepted"], yearly
    assert first["generation"]["rows"] == [["202401", 15.0], ["202402", 20.0]]
    assert yearly["generation"]["rows"] == [["2024", 35.0]]
    assert yearly["state"]["semantic_contract"]["grain"] == ["year"]
    assert yearly["result_id"] != first["result_id"] and len(sql.calls) == top.calls == 2
    assert yearly["state"]["version"] > first["state"]["version"]
    for index, response in enumerate((first, yearly)):
        assert normalize_sql(response["generation"]["sql"]) == normalize_sql(sql.statements[index])
        assert normalize_sql(response["state"]["last_sql"]) == normalize_sql(sql.statements[index])
        ledger = response["call_ledger"]
        components = [entry["component"] for entry in ledger]
        assert components.index("sql_generation") < components.index("sql_execution")
        assert any(entry["component"] == "assistant_tool:ask_data"
                   and entry["status"] == "SUCCEEDED" for entry in ledger)


def test_country_comparison_then_percentage_preserves_period_and_population(
    tmp_path, monkeypatch,  # type: ignore[no-untyped-def]
):
    top = ScriptedCompleter([
        decision("Germany consumption for 2024", first=True, changes=[
            {"operation": "ADD_FILTER", "target": "customers.Country", "new_value": "Germany"},
            {"operation": "CHANGE_PERIOD", "target": "period", "new_value": "2024"}]),
        decision("Compare France with Germany in the same year", changes=[
            {"operation": "COMPARE", "target": "customers.Country", "new_value": "France"},
            {"operation": "ADD_DIMENSION", "target": "Country", "new_value": "Country"}]),
        decision("Percentage consumption difference Germany relative to France", changes=[
            {"operation": "CHANGE_METRIC", "target": "consumption",
             "new_value": "percentage_difference"},
            {"operation": "REMOVE_DIMENSION", "target": "Country"}]),
    ])
    common = (" FROM yearmonth y JOIN customers c ON c.CustomerID=y.CustomerID "
              "WHERE y.Date >= '202401' AND y.Date < '202501' ")
    sql = SQLSequence([
        "SELECT SUM(y.Consumption) AS total" + common + "AND c.Country='Germany'",
        "SELECT c.Country, SUM(y.Consumption) AS total" + common
        + "AND c.Country IN ('Germany','France') GROUP BY c.Country ORDER BY c.Country",
        "SELECT 100.0*(SUM(CASE WHEN c.Country='Germany' THEN y.Consumption ELSE 0 END)-"
        "SUM(CASE WHEN c.Country='France' THEN y.Consumption ELSE 0 END))/"
        "NULLIF(SUM(CASE WHEN c.Country='France' THEN y.Consumption ELSE 0 END),0) AS difference"
        + common + "AND c.Country IN ('Germany','France')",
    ])
    client = _client(tmp_path, monkeypatch, top, ScriptedCompleter([]), sql)
    with sqlite3.connect(tmp_path / "consumption" / "consumption.sqlite") as connection:
        connection.execute("ALTER TABLE customers ADD COLUMN Country TEXT")
        connection.execute("UPDATE customers SET Country=CASE WHEN CustomerID=1 "
                           "THEN 'Germany' ELSE 'France' END")
    first = _post(client, "Consumption in Germany in 2024", "country-comparison")
    compared = _post(client, "Compare France", "country-comparison")
    percent = _post(client, "Show percentage difference relative to France", "country-comparison")
    assert all(item["generation"]["accepted"] for item in (first, compared, percent)), percent
    assert first["generation"]["rows"] == [[30.0]]
    assert compared["generation"]["rows"] == [["France", 5.0], ["Germany", 30.0]]
    assert percent["generation"]["rows"] == [[500.0]]
    contract = percent["state"]["semantic_contract"]
    assert contract["active_period"] == "2024"
    assert contract["filters"][0]["operator"] == "IN"
    assert contract["filters"][0]["value"] == ["Germany", "France"]
    assert len({item["result_id"] for item in (first, compared, percent)}) == 3
    assert top.calls == len(sql.calls) == 3
    for index, response in enumerate((first, compared, percent)):
        assert normalize_sql(response["generation"]["sql"]) == normalize_sql(sql.statements[index])
        assert normalize_sql(response["state"]["last_sql"]) == normalize_sql(sql.statements[index])
