"""Adversarial executable shapes must not masquerade as semantic evidence."""
import pytest

from semantic_text2sql.models import ApprovedMetricDefinition, SemanticContract, SemanticFilter
from semantic_text2sql.validator import validate_sql_semantics


def checks(sql, **requirements):  # type: ignore[no-untyped-def]
    return {item.code: item.status for item in validate_sql_semantics(
        sql, SemanticContract(**requirements),
        {"tables": {"sales": {"columns": {"price": {}, "amount": {}}}}},
    )}


@pytest.mark.parametrize("sql,status", [
    ("SELECT date, SUM(price) FROM sales GROUP BY date", "PASS"),
    ("SELECT SUBSTR(date,1,4), SUM(price) FROM sales GROUP BY SUBSTR(date,1,4)", "FAIL"),
    ("SELECT date AS d, SUM(price) FROM sales GROUP BY d", "PASS"),
    ("WITH x AS (SELECT date AS d, price FROM sales) "
     "SELECT d, SUM(price) FROM x GROUP BY d", "PASS"),
])
def test_final_grain_not_just_column_presence(sql, status):  # type: ignore[no-untyped-def]
    result = checks(sql, requires_grouping=True, grain=["date"])
    assert result.get("GROUPING_DIMENSIONS_PRESENT",
                      result.get("GROUPING_DIMENSION_MISMATCH")) == status


@pytest.mark.parametrize("predicate,status", [
    ("country = 'DE'", "PASS"), ("'DE' = country", "PASS"),
    ("country IN ('DE')", "PASS"), ("NOT(country = 'DE')", "UNRESOLVED"),
    ("country = 'de'", "UNRESOLVED"), ("country = 'DE' OR price > 0", "UNRESOLVED"),
])
def test_predicate_direction_negation_and_case(predicate, status):  # type: ignore[no-untyped-def]
    result = checks("SELECT price FROM sales WHERE " + predicate,
                    filters=[SemanticFilter(operand="country", operator="=", value="DE")])
    assert result.get("FILTER_PREDICATE_MATCH",
                      result.get("REQUIRED_FILTER_PREDICATE_MISMATCH")) == status


def test_unused_cte_cannot_supply_outputs_or_filters() -> None:
    result = checks("WITH unused AS (SELECT price AS expected FROM sales WHERE country='DE') "
                    "SELECT amount FROM sales", required_output_aliases=["expected"],
                    filters=[SemanticFilter(operand="country", operator="=", value="DE")])
    assert result["REQUIRED_OUTPUT_MISSING"] == "FAIL"
    assert result["REQUIRED_FILTER_VALUE_MISSING"] == "FAIL"


def test_between_preserves_bound_order_and_supports_conjunction() -> None:
    requirement = [SemanticFilter(operand="date", operator="BETWEEN", value=["201301", "201312"])]
    good = checks("SELECT price FROM sales WHERE date >= '201301' AND date <= '201312'",
                  filters=requirement)
    bad = checks("SELECT price FROM sales WHERE date BETWEEN '201312' AND '201301'",
                 filters=requirement)
    assert good["FILTER_PREDICATE_MATCH"] == "PASS"
    assert bad["REQUIRED_FILTER_PREDICATE_MISMATCH"] == "FAIL"


def test_derived_approved_formula_through_cte_and_wrong_source_measure() -> None:
    metric = ApprovedMetricDefinition(id="unit_price", formula="SUM(price)/NULLIF(SUM(amount),0)",
                                      source_relation="sales", required_fields=["price", "amount"])
    result = checks("WITH x AS (SELECT price AS p, amount AS a FROM sales) "
                    "SELECT SUM(p)/NULLIF(SUM(a),0) FROM x", approved_metrics=[metric])
    assert result["APPROVED_METRIC_FORMULA_MATCH"] == "PASS"
    wrong = checks("SELECT SUM(t.price) FROM other t", aggregation="sum", measures=["sales.price"])
    assert wrong["AGGREGATED_MEASURE_MISMATCH"] == "FAIL"


def test_unused_aggregate_cannot_prove_final_aggregation() -> None:
    result = checks("WITH unused AS (SELECT SUM(price) AS total FROM sales) "
                    "SELECT amount FROM sales", aggregation="sum")
    assert result["AGGREGATION_NOT_FOUND"] == "FAIL"


def test_additional_grouping_is_not_silently_verified() -> None:
    result = checks("SELECT country, SUM(price) FROM sales GROUP BY country, status",
                    requires_grouping=True, output_grain=["country"])
    assert result["FINAL_GRAIN_EQUIVALENCE_UNPROVEN"] == "UNRESOLVED"


def test_transformed_filter_is_unresolved_not_rejected_as_missing() -> None:
    result = checks("SELECT price FROM sales WHERE LOWER(country)='de'", filters=[
        SemanticFilter(operand="country", operator="=", value="DE")])
    assert result["REQUIRED_FILTER_VALUE_MISSING"] == "UNRESOLVED"


@pytest.mark.parametrize("sql,code", [
    ("SELECT SUM(c.amount) FROM child c JOIN parent p ON c.id=p.id AND c.part=p.part",
     "JOIN_MULTIPLICITY_PRESERVED"),
    ("SELECT COUNT(DISTINCT p.id,p.part) FROM child c JOIN parent p "
     "ON c.id=p.id AND c.part=p.part", "DISTINCT_ENTITY_MULTIPLICITY_PRESERVED"),
    ("SELECT SUM(c.amount) FROM child c JOIN parent p ON c.id=p.id", None),
])
def test_composite_join_requires_complete_keys(sql, code):  # type: ignore[no-untyped-def]
    context = {
        "tables": {"parent": {"primary_key": ["id", "part"]}, "child": {}},
        "relationships": [{"left": "child.id", "right": "parent.id",
                           "left_columns": ["child.id", "child.part"],
                           "right_columns": ["parent.id", "parent.part"],
                           "right_key_unique": True, "fanout_risk": True}],
    }
    findings = validate_sql_semantics(sql, SemanticContract(), context)
    if code:
        assert any(item.code == code and item.status == "PASS" for item in findings)
    else:
        assert any(item.status == "UNRESOLVED" for item in findings)
        assert not any(item.status == "PASS" for item in findings)


def test_lineage_expansion_is_bounded_and_reports_unknown() -> None:
    from sqlglot import parse_one

    from semantic_text2sql.semantic_lineage import ExpressionLineage

    ctes = ["t0 AS (SELECT price AS x FROM sales)"]
    ctes += [f"t{i} AS (SELECT x+x AS x FROM t{i-1})" for i in range(1, 24)]
    lineage = ExpressionLineage(parse_one("WITH " + ",".join(ctes) + " SELECT x FROM t23"))
    assert lineage.output_expressions() == [None]
    assert lineage.remaining_nodes >= 0
