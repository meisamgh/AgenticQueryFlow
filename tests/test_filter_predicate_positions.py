"""A comparison inside an expression is not necessarily an enforced filter."""
import pytest

from semantic_text2sql.models import SemanticContract, SemanticFilter
from semantic_text2sql.validator import validate_sql_semantics


@pytest.mark.parametrize("where", [
    "CASE WHEN status='complete' THEN 1 ELSE 1 END=1",
    "CAST(status='complete' AS INTEGER)>=0",
    "(status='complete') IS FALSE",
    "COALESCE(status='complete', TRUE)",
    "(status='complete') = (amount>0)",
])
def test_wrapped_comparisons_do_not_certify_mandatory_filters(where: str) -> None:
    contract = SemanticContract(mandatory_metric_filters=[SemanticFilter(
        operand="orders.status", operator="=", value="complete")])
    findings = validate_sql_semantics(f"SELECT SUM(amount) FROM orders WHERE {where}", contract)
    matching = [item for item in findings if "FILTER" in item.code]
    assert matching and all(item.status == "UNRESOLVED" for item in matching)


def test_hidden_bounds_do_not_certify_between() -> None:
    contract = SemanticContract(filters=[SemanticFilter(operand="orders.amount", operator="BETWEEN",
                                                        value=[10, 20])])
    findings = validate_sql_semantics(
        "SELECT amount FROM orders WHERE CASE WHEN amount>=10 THEN 1 ELSE 1 END=1 "
        "AND CASE WHEN amount<=20 THEN 1 ELSE 1 END=1", contract)
    assert any(item.status == "UNRESOLVED" and "FILTER" in item.code for item in findings)
    assert not any(item.status == "PASS" and "FILTER" in item.code for item in findings)


def test_negated_alternate_operator_is_unresolved_not_incorrectly_failed() -> None:
    contract = SemanticContract(filters=[SemanticFilter(operand="orders.status", operator="!=",
                                                        value="pending")])
    findings = validate_sql_semantics(
        "SELECT amount FROM orders WHERE NOT(status='pending')", contract)
    assert any(item.status == "UNRESOLVED" and "FILTER" in item.code for item in findings)
    assert not any(item.status == "FAIL" for item in findings)


def test_real_positive_conjunct_still_proves_filter_with_irrelevant_wrapped_comparison() -> None:
    contract = SemanticContract(filters=[SemanticFilter(operand="orders.status", operator="=",
                                                        value="complete")])
    findings = validate_sql_semantics(
        "SELECT amount FROM orders WHERE (status='complete') AND "
        "CASE WHEN status='complete' THEN 1 ELSE 1 END=1", contract)
    assert any(item.status == "PASS" and item.code == "FILTER_PREDICATE_MATCH"
               for item in findings)
