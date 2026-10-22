"""Verified filter-classification contracts under realistic shapes."""

from __future__ import annotations

from semantic_text2sql.filter_discovery import discover_filters
from semantic_text2sql.models import ColumnInfo, ForeignKeyInfo, SchemaInfo, TableInfo

COLUMNS = ["month", "country", "orders"]


class _Profile:
    def __init__(self, table: str, column: str, distinct_count: int,
                 semantic_type: str = "text") -> None:
        self.table = table
        self.column = column
        self.distinct_count = distinct_count
        self.semantic_type = semantic_type


class _ProfileHolder:
    def __init__(self, columns) -> None:
        self.columns = columns


def _rows(rows):
    return rows


def test_result_column_classified_as_client_filter() -> None:
    catalog = discover_filters(
        schema=SchemaInfo(db_id="d", tables=[], relationships=[]),
        columns=["month", "country", "orders"],
        rows=[[m, c, 5] for m in ("202501",) for c in ("Germany", "France", "Spain")],
        truncated=False,
    )
    assert [item.field for item in catalog.client_filters] == ["month", "country"]
    assert catalog.client_filters[0].exec_mode == "CLIENT_FILTER"
    assert catalog.client_filters[1].control == "multi_select"
    assert all(item.source == "stored_result" for item in catalog.client_filters)


def test_truncated_result_demotes_client_filters() -> None:
    catalog = discover_filters(
        schema=SchemaInfo(db_id="d", tables=[], relationships=[]),
        columns=["country", "orders"],
        rows=[["Germany", 5], ["France", 4]],
        truncated=True,
    )
    assert all(item.exec_mode == "UNAVAILABLE" for item in catalog.client_filters)
    assert catalog.sql_filters == []


def test_aggregate_result_columns_never_become_filters() -> None:
    catalog = discover_filters(
        schema=SchemaInfo(db_id="d", tables=[], relationships=[]),
        columns=["orders", "revenue"],
        rows=[[5, 100.0]],
        truncated=False,
    )
    assert catalog.client_filters == []


def test_sql_filter_needs_verified_table_and_enjoys_join_path() -> None:
    schema = SchemaInfo(
        db_id="d",
        tables=[
            TableInfo(
                name="orders",
                create_sql="CREATE TABLE orders (order_id INTEGER PRIMARY KEY, month TEXT, "
                "country TEXT, status TEXT)",
                columns=[
                    ColumnInfo(name="order_id", data_type="INTEGER", primary_key=True),
                    ColumnInfo(name="month", data_type="TEXT"),
                    ColumnInfo(name="country", data_type="TEXT"),
                    ColumnInfo(name="status", data_type="TEXT"),
                ],
            ),
            TableInfo(
                name="customers",
                create_sql="CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, "
                "name TEXT)",
                columns=[
                    ColumnInfo(name="customer_id", data_type="INTEGER", primary_key=True),
                    ColumnInfo(name="name", data_type="TEXT"),
                ],
            ),
        ],
        relationships=[
            ForeignKeyInfo(from_table="orders", from_column="order_id", to_table="customers",
                           to_column="customer_id"),
        ],
    )
    catalog = discover_filters(
        schema=schema,
        columns=["country", "orders"],
        rows=[["Germany", 5]],
        truncated=False,
        approved_tables=["orders", "customers"],
    )
    fields = {(item.field, item.table) for item in catalog.sql_filters}
    assert ("orders.month", "orders") in fields
    assert ("orders.status", "orders") in fields
    assert any(item.column == "name" for item in catalog.sql_filters)
    assert all(item.exec_mode == "SQL_FILTER" and item.source == "verified_schema"
               for item in catalog.sql_filters)
    assert not any(item.column in {"order_id", "customer_id"} for item in catalog.sql_filters)


def test_high_cardinality_text_column_is_unavailable() -> None:
    schema = SchemaInfo(
        db_id="d",
        tables=[
            TableInfo(
                name="orders",
                create_sql="CREATE TABLE orders (description TEXT)",
                columns=[ColumnInfo(name="description", data_type="TEXT")],
            )
        ],
        relationships=[],
    )
    catalog = discover_filters(
        schema=schema,
        columns=["orders"],
        rows=[[5]],
        truncated=False,
        approved_tables=["orders"],
        profile=_ProfileHolder([_Profile("orders", "description", 750)]),
    )
    assert catalog.unavailable_filters, "high-cardinality column must surface as unavailable"


def test_identifier_and_primary_key_columns_excluded() -> None:
    schema = SchemaInfo(
        db_id="d",
        tables=[
            TableInfo(
                name="orders",
                create_sql="CREATE TABLE orders (order_id INTEGER PRIMARY KEY, code TEXT)",
                columns=[
                    ColumnInfo(name="order_id", data_type="INTEGER", primary_key=True,
                               semantic_type="identifier"),
                    ColumnInfo(name="code", data_type="TEXT", semantic_type="identifier"),
                ],
            )
        ],
        relationships=[],
    )
    catalog = discover_filters(
        schema=schema,
        columns=["orders"],
        rows=[[1]],
        truncated=False,
        approved_tables=["orders"],
    )
    assert catalog.sql_filters == []
    assert catalog.unavailable_filters == []


def test_sensitive_column_becomes_unavailable_never_proposed() -> None:
    schema = SchemaInfo(
        db_id="d",
        tables=[
            TableInfo(
                name="customers",
                create_sql="CREATE TABLE customers (contact_email TEXT)",
                columns=[ColumnInfo(name="contact_email", data_type="TEXT",
                                    semantic_type="email")],
            )
        ],
        relationships=[],
    )
    catalog = discover_filters(
        schema=schema,
        columns=["orders"],
        rows=[[1]],
        truncated=False,
        approved_tables=["customers"],
        profile=_ProfileHolder([_Profile("customers", "contact_email", 50,
                                         semantic_type="email")]),
    )
    item = catalog.unavailable_filters[0]
    assert item.exec_mode == "UNAVAILABLE"
    assert item.column == "contact_email"


def test_numeric_source_columns_not_proposed_as_sql_filters() -> None:
    schema = SchemaInfo(
        db_id="d",
        tables=[
            TableInfo(
                name="orders",
                create_sql="CREATE TABLE orders (amount REAL)",
                columns=[ColumnInfo(name="amount", data_type="REAL")],
            )
        ],
        relationships=[],
    )
    catalog = discover_filters(
        schema=schema,
        columns=["orders"],
        rows=[[1]],
        truncated=False,
        approved_tables=["orders"],
    )
    assert catalog.sql_filters == []


def test_month_like_column_gets_a_date_control() -> None:
    catalog = discover_filters(
        schema=SchemaInfo(db_id="d", tables=[], relationships=[]),
        columns=["order_month", "orders"],
        rows=[["202501", 5], ["202502", 3], ["202503", 7]],
        truncated=False,
    )
    assert catalog.client_filters[0].control == "date_range"
