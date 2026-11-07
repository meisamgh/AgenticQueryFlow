"""Reproductions for scoped authorization defects in the current worktree."""
import pytest

from semantic_text2sql.validator import validate_sql


@pytest.mark.parametrize("sql", [
    "SELECT p.country FROM customers p WHERE EXISTS(SELECT 1 FROM orders p)",
    "SELECT country AS x FROM customers WHERE EXISTS(SELECT 1 AS country FROM orders)",
    "SELECT c.country FROM customers c",
    "SELECT country FROM customers",
])
def test_column_grant_is_scoped_to_actual_relation(registry, sql) -> None:  # type: ignore[no-untyped-def]
    result = validate_sql(sql, registry.inspect("shop"), allowed_columns={
        "customers": {"customer_id"}, "orders": {"country", "order_id"},
    })
    assert result.code == "SQL_COLUMN_NOT_AUTHORIZED", result


def test_missing_column_grant_fails_closed(registry) -> None:  # type: ignore[no-untyped-def]
    result = validate_sql("SELECT c.name FROM customers c", registry.inspect("shop"),
                          allowed_columns={"orders": {"order_id"}})
    assert result.code == "SQL_COLUMN_NOT_AUTHORIZED"


@pytest.mark.parametrize("sql", [
    "SELECT c.customer_id AS id FROM customers c ORDER BY id",
    "WITH x AS (SELECT customer_id AS id FROM customers) SELECT id FROM x",
    "SELECT c.customer_id FROM customers c WHERE EXISTS "
    "(SELECT 1 FROM orders o WHERE o.customer_id=c.customer_id)",
])
def test_scoped_authorization_preserves_valid_aliases_and_correlations(registry, sql) -> None:  # type: ignore[no-untyped-def]
    result = validate_sql(sql, registry.inspect("shop"), allowed_columns={
        "customers": {"customer_id"}, "orders": {"customer_id"},
    })
    assert result.valid, result
