"""High-confidence semantic checks reject only provable contract violations."""

from __future__ import annotations

from semantic_text2sql.models import SemanticContract, SemanticFilter
from semantic_text2sql.validator import validate_sql_semantics


def _codes(sql: str, contract: SemanticContract) -> dict[str, str]:
    return {item.code: item.status for item in validate_sql_semantics(sql, contract)}


def test_missing_required_group_by_is_rejected() -> None:
    contract = SemanticContract(aggregation="sum", requires_grouping=True, grain=["Currency"])
    result = _codes("SELECT Currency, SUM(Consumption) FROM sales", contract)
    assert result["GROUPING_NOT_FOUND"] == "FAIL"
    assert result["AGGREGATION_PRESENT"] == "PASS"


def test_wrong_aggregate_and_missing_filter_are_rejected() -> None:
    contract = SemanticContract(
        aggregation="sum",
        filters=[SemanticFilter(operand="Country", operator="=", value="Germany")],
    )
    result = _codes("SELECT AVG(Consumption) FROM sales WHERE Country = 'France'", contract)
    assert result["AGGREGATION_NOT_FOUND"] == "FAIL"
    assert result["REQUIRED_FILTER_VALUE_MISSING"] == "FAIL"


def test_valid_grouping_and_filter_do_not_raise_semantic_failure() -> None:
    contract = SemanticContract(
        aggregation="sum", requires_grouping=True, grain=["Currency"],
        filters=[SemanticFilter(operand="Country", operator="=", value="Germany")],
    )
    result = _codes(
        "SELECT Currency, SUM(Consumption) FROM sales "
        "WHERE Country = 'Germany' GROUP BY Currency",
        contract,
    )
    assert all(status != "FAIL" for status in result.values())
    assert result["GROUPING_PRESENT"] == "PASS"
    assert result["FILTER_PREDICATE_UNVERIFIED"] == "UNRESOLVED"


def test_alternate_average_formula_is_unresolved_not_falsely_rejected() -> None:
    contract = SemanticContract(aggregation="average")
    assert _codes("SELECT SUM(Value) / NULLIF(COUNT(*), 0) FROM sales", contract)[
        "AGGREGATION_NOT_FOUND"
    ] == "UNRESOLVED"
