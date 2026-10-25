"""High-confidence semantic checks reject only provable contract violations."""

from __future__ import annotations

from semantic_text2sql.models import ApprovedMetricDefinition, SemanticContract, SemanticFilter
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
    assert result["REQUIRED_FILTER_PREDICATE_MISMATCH"] == "FAIL"


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
    assert result["GROUPING_DIMENSIONS_PRESENT"] == "PASS"
    assert result["FILTER_PREDICATE_MATCH"] == "PASS"


def test_alternate_average_formula_is_unresolved_not_falsely_rejected() -> None:
    contract = SemanticContract(aggregation="average")
    assert _codes("SELECT SUM(Value) / NULLIF(COUNT(*), 0) FROM sales", contract)[
        "AGGREGATION_NOT_FOUND"
    ] == "UNRESOLVED"


def test_ranked_metric_must_match_final_order_key() -> None:
    contract = SemanticContract(
        requires_ordering=True, ranking_direction="DESC",
        ranked_metric="total_spending", limit=1,
    )
    wrong = _codes(
        "SELECT customer_id, SUM(price) AS total_spending, SUM(amount) AS quantity "
        "FROM sales GROUP BY customer_id ORDER BY quantity DESC LIMIT 1", contract,
    )
    assert wrong["RANKED_METRIC_MISMATCH"] == "FAIL"
    correct = _codes(
        "SELECT customer_id, SUM(price) AS total_spending "
        "FROM sales GROUP BY customer_id ORDER BY total_spending DESC LIMIT 1", contract,
    )
    assert correct["RANKED_METRIC_MATCH"] == "PASS"


def _verified_context() -> dict[str, object]:
    return {
        "tables": {
            "sales": {"columns": {"Currency": {}, "Country": {}, "Consumption": {},
                                  "Amount": {}}},
            "transactions_1k": {"columns": {"CustomerID": {}, "Price": {}}},
            "yearmonth": {"columns": {"CustomerID": {}, "Consumption": {}}},
        },
        "relationships": [{
            "left": "transactions_1k.CustomerID", "right": "yearmonth.CustomerID",
            "cardinality": "MANY_TO_MANY", "fanout_risk": True,
        }],
    }


def test_group_by_wrong_dimension_is_provable_failure() -> None:
    contract = SemanticContract(requires_grouping=True, grain=["Currency"])
    result = validate_sql_semantics(
        "SELECT Country, SUM(Consumption) FROM sales GROUP BY Country",
        contract, _verified_context(),
    )
    assert any(item.code == "GROUPING_DIMENSION_MISMATCH" and item.status == "FAIL"
               for item in result)


def test_group_by_alias_or_ordinal_resolves_simple_column() -> None:
    contract = SemanticContract(requires_grouping=True, grain=["Currency"])
    for sql in (
        "SELECT Currency AS customer_currency, SUM(Consumption) FROM sales "
        "GROUP BY customer_currency",
        "SELECT Currency, SUM(Consumption) FROM sales GROUP BY 1",
    ):
        result = validate_sql_semantics(sql, contract, _verified_context())
        assert any(item.code == "GROUPING_DIMENSIONS_PRESENT" and item.status == "PASS"
                   for item in result)


def test_wrong_physical_measure_is_provable_failure() -> None:
    contract = SemanticContract(aggregation="sum", measures=["Consumption"])
    result = validate_sql_semantics(
        "SELECT SUM(Amount) FROM sales", contract, _verified_context(),
    )
    assert any(item.code == "AGGREGATED_MEASURE_MISMATCH" and item.status == "FAIL"
               for item in result)


def test_filter_literal_in_unrelated_predicate_is_not_accepted() -> None:
    contract = SemanticContract(filters=[
        SemanticFilter(operand="Country", operator="=", value="Germany"),
    ])
    result = validate_sql_semantics(
        "SELECT Country FROM sales WHERE Country = 'France' AND Currency = 'Germany'",
        contract, _verified_context(),
    )
    assert any(item.code == "REQUIRED_FILTER_PREDICATE_MISMATCH" and item.status == "FAIL"
               for item in result)


def test_qualified_filter_is_not_satisfied_by_same_named_column_on_other_table() -> None:
    contract = SemanticContract(filters=[
        SemanticFilter(operand="customers.Country", operator="=", value="Germany"),
    ])
    result = validate_sql_semantics(
        "SELECT c.CustomerID FROM customers c JOIN gasstations g ON 1 = 1 "
        "WHERE g.Country = 'Germany'",
        contract, _verified_context(),
    )
    assert any(item.code == "REQUIRED_FILTER_VALUE_MISSING" and item.status == "FAIL"
               for item in result)


def test_direct_fact_join_aggregate_is_flagged_without_false_proof() -> None:
    result = validate_sql_semantics(
        "SELECT SUM(y.Consumption) FROM transactions_1k t "
        "JOIN yearmonth y ON t.CustomerID = y.CustomerID",
        SemanticContract(), _verified_context(),
    )
    assert any(item.code == "FACT_JOIN_FANOUT_RISK" and item.status == "UNRESOLVED"
               for item in result)


def test_preaggregated_fact_join_requires_review_not_false_rejection() -> None:
    result = validate_sql_semantics(
        "WITH monthly AS (SELECT CustomerID, SUM(Consumption) AS total "
        "FROM yearmonth GROUP BY CustomerID) "
        "SELECT SUM(monthly.total) FROM transactions_1k t "
        "JOIN monthly ON t.CustomerID = monthly.CustomerID",
        SemanticContract(), _verified_context(),
    )
    assert not any(item.code == "FACT_JOIN_FANOUT_RISK" and item.status == "FAIL"
                   for item in result)


def test_explicit_temporal_boundaries_are_checked_against_sql() -> None:
    contract = SemanticContract(temporal_boundaries=[
        SemanticFilter(operand="yearmonth.Date", operator=">=", value="201301"),
        SemanticFilter(operand="yearmonth.Date", operator="<", value="201401"),
    ])
    correct = _codes(
        "SELECT SUM(Consumption) FROM yearmonth "
        "WHERE Date >= '201301' AND Date < '201401'", contract,
    )
    wrong = _codes(
        "SELECT SUM(Consumption) FROM yearmonth "
        "WHERE Date >= '201201' AND Date < '201401'", contract,
    )
    assert correct["FILTER_PREDICATE_MATCH"] == "PASS"
    assert wrong["REQUIRED_FILTER_PREDICATE_MISMATCH"] == "FAIL"


def test_transformed_temporal_boundary_is_unresolved_not_rejected() -> None:
    contract = SemanticContract(temporal_boundaries=[
        SemanticFilter(operand="yearmonth.Date", operator=">=", value="201301"),
    ])
    result = _codes(
        "WITH periods AS (SELECT Date, Consumption FROM yearmonth) "
        "SELECT SUM(Consumption) FROM periods WHERE Date >= '201301'", contract,
    )
    assert result["REQUIRED_FILTER_VALUE_MISSING"] == "UNRESOLVED"


def test_direct_ranking_direction_and_limit_are_checked() -> None:
    contract = SemanticContract(requires_ordering=True, ranking_direction="DESC", limit=3)
    correct = _codes("SELECT CustomerID FROM customers ORDER BY Score DESC LIMIT 3", contract)
    wrong = _codes("SELECT CustomerID FROM customers ORDER BY Score ASC LIMIT 5", contract)
    assert correct["RANKING_DIRECTION_MATCH"] == "PASS"
    assert correct["RANKING_LIMIT_MATCH"] == "PASS"
    assert wrong["RANKING_DIRECTION_MISMATCH"] == "FAIL"
    assert wrong["RANKING_LIMIT_MISMATCH"] == "FAIL"


def test_window_rank_without_outer_order_is_not_falsely_rejected() -> None:
    contract = SemanticContract(requires_ordering=True, ranking_direction="DESC", limit=1)
    result = _codes(
        "WITH ranked AS (SELECT CustomerID, ROW_NUMBER() OVER "
        "(ORDER BY Score DESC) AS rn FROM customers) "
        "SELECT CustomerID FROM ranked WHERE rn = 1", contract,
    )
    assert result["RANKING_ORDER_MISSING"] == "UNRESOLVED"
    assert result["RANKING_LIMIT_MISSING"] == "UNRESOLVED"


def test_simple_approved_metric_input_is_proven_or_rejected() -> None:
    contract = SemanticContract(approved_metrics=[ApprovedMetricDefinition(
        id="total_spending", formula="SUM(Price)",
        source_relation="transactions_1k", required_fields=["Price"],
    )])
    good = _codes("SELECT SUM(t.Price) FROM transactions_1k AS t", contract)
    bad = _codes("SELECT SUM(t.Amount) FROM transactions_1k AS t", contract)
    transformed = _codes(
        "SELECT SUM(CAST(t.Price AS REAL)) FROM transactions_1k AS t", contract,
    )
    ambiguous = _codes(
        "SELECT SUM(t.Price), SUM(t.Amount) FROM transactions_1k AS t", contract,
    )
    assert good["APPROVED_METRIC_INPUT_MATCH"] == "PASS"
    assert bad["APPROVED_METRIC_INPUT_MISMATCH"] == "FAIL"
    assert transformed["APPROVED_METRIC_INPUT_MISMATCH"] == "UNRESOLVED"
    assert ambiguous["METRIC_FORMULA_EQUIVALENCE_UNPROVEN"] == "UNRESOLVED"


def test_derived_metric_formula_stays_unresolved() -> None:
    contract = SemanticContract(approved_metrics=[ApprovedMetricDefinition(
        id="margin", formula="SUM(Profit) / NULLIF(SUM(Revenue), 0)",
        source_relation="sales", required_fields=["Profit", "Revenue"],
    )])
    result = _codes("SELECT SUM(Profit) / SUM(Revenue) FROM sales", contract)
    assert result["METRIC_FORMULA_EQUIVALENCE_UNPROVEN"] == "UNRESOLVED"
