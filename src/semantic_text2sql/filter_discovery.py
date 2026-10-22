"""Verified filter discovery with safe execution classification.

Proposed filters are classified purely from verified metadata — the complete
executed result, the live schema, column profiles, and approved source tables.
The discovery runs no SQL and never proposes a filter field that is not
verified by one of those sources; value discovery stays a separate bounded,
read-only endpoint.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from semantic_text2sql.models import (
    ColumnInfo,
    ColumnProfile,
    DatabaseProfile,
    FilterCatalog,
    SchemaInfo,
    SuggestedFilter,
)

if TYPE_CHECKING:  # pragma: no cover - type-only import
    from collections.abc import Sequence

_SENSITIVE_TYPES = {"secret", "email", "phone", "postal_address", "password"}
_MULTI_SELECT_LIMIT = 12
_DATE_NAME = r"(?:^|_)(?:date|time|month|year|week|quarter|day)(?:$|_)"

_UNSAFE_NAME = r"(?:^|_)(?:id|code|uuid|guid|token|password|email|phone|address|ssn)(?:$|_)"


def _result_control(name: str, distinct: int) -> str | None:
    lowered = name.casefold()
    if re.search(_DATE_NAME, lowered) and lowered not in {"year"}:
        return "date_range"
    if re.search(_DATE_NAME, lowered):
        return "multi_select"
    if distinct <= 1:
        return None
    if distinct <= _MULTI_SELECT_LIMIT:
        return "multi_select"
    return None


def _profile_column(
    profile: DatabaseProfile | None,
    table: str | None,
    column: str,
) -> ColumnProfile | None:
    if profile is None or table is None:
        return None
    columns = profile.columns
    for item in columns:
        if item.column == column and item.table == table:
            return item
    return next((item for item in columns if item.column == column), None)


def _schema_tables(schema: SchemaInfo) -> dict[str, Sequence[ColumnInfo]]:
    return {table.name: table.columns for table in schema.tables}


def discover_filters(
    *,
    schema: SchemaInfo,
    columns: list[str],
    rows: list[list[object]],
    truncated: bool,
    approved_tables: list[str] | None = None,
    profile: DatabaseProfile | None = None,
) -> FilterCatalog:
    """Classify trustworthy filters from the result and verified metadata.

    The result must be complete: a truncated preview cannot back a
    client-filter that hides rows.
    """
    catalog = FilterCatalog()
    result_columns = {
        name: index for index, name in enumerate(columns)
    }

    # CLIENT_FILTERS: only fields physically present in the complete result can
    # be narrowed without changing analytical meaning or running SQL.
    for name, index in result_columns.items():
        values = [row[index] for row in rows]
        populated = [value for value in values if value is not None]
        numeric_only = populated and all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in populated
        )
        if numeric_only:
            # Result measures are analysis outputs, never filter fields.
            continue
        result_distinct = len({str(value) for value in populated})
        result_control = _result_control(name, result_distinct)
        if result_control is None or re.search(_UNSAFE_NAME, name.casefold()):
            continue
        catalog.client_filters.append(
            SuggestedFilter(
                field=name,
                source="stored_result",
                exec_mode="CLIENT_FILTER" if not truncated else "UNAVAILABLE",
                control=result_control,  # type: ignore[arg-type]
                label=name.replace("_", " "),
                cardinality=result_distinct,
                searchable=result_distinct > _MULTI_SELECT_LIMIT,
                reason="present in the complete executed result; narrows visible rows only",
            )
        )

    tables = _schema_tables(schema)
    scope = approved_tables or list(tables)
    scope = [name for name in scope if name in tables][:6]

    # SQL_FILTERS: verified source dimensions that need a validated new query.
    for table_name in scope:
        for column in tables[table_name]:
            if column.name in result_columns or column.primary_key:
                continue
            column_profile = _profile_column(profile, table_name, column.name)
            semantic = (
                column_profile.semantic_type if column_profile else None
            ) or column.semantic_type
            if semantic in _SENSITIVE_TYPES:
                catalog.unavailable_filters.append(
                    SuggestedFilter(
                        field=f"{table_name}.{column.name}",
                        source="verified_schema",
                        table=table_name,
                        column=column.name,
                        exec_mode="UNAVAILABLE",
                        control="multi_select",
                        label=column.name.replace("_", " "),
                        reason="the field is sensitive and excluded from filter discovery",
                    )
                )
                continue
            if re.search(_UNSAFE_NAME, column.name.casefold()):
                continue
            if semantic in {"identifier", "key"}:
                continue
            source_distinct: int | None = (
                column_profile.distinct_count if column_profile else None
            )
            physical = column.data_type.casefold().split("(", 1)[0]
            textual = physical.startswith(("text", "varchar", "char")) or physical in {
                "date", "datetime", "timestamp"
            }
            if not textual:
                continue
            if source_distinct is not None and source_distinct > 500:
                catalog.unavailable_filters.append(
                    SuggestedFilter(
                        field=f"{table_name}.{column.name}",
                        source="verified_schema",
                        table=table_name,
                        column=column.name,
                        exec_mode="UNAVAILABLE",
                        control="search",
                        label=column.name.replace("_", " "),
                        cardinality=source_distinct,
                        reason="the value set is too large to be a practical filter",
                    )
                )
                continue
            control: str
            date_like = physical in {"date", "datetime", "timestamp"} or semantic == "date"
            if date_like or re.search(_DATE_NAME, column.name.casefold()):
                control = "date_range"
            elif source_distinct is not None and source_distinct <= _MULTI_SELECT_LIMIT:
                control = "multi_select"
            else:
                control = "search"
            relationships = [
                f"{item.from_table}.{item.from_column}->{item.to_table}.{item.to_column}"
                for item in schema.relationships
                if item.from_table == table_name and item.from_table in scope
            ]
            catalog.sql_filters.append(
                SuggestedFilter(
                    field=f"{table_name}.{column.name}",
                    source="verified_schema",
                    table=table_name,
                    column=column.name,
                    exec_mode="SQL_FILTER",
                    control=control,  # type: ignore[arg-type]
                    label=column.name.replace("_", " "),
                    cardinality=source_distinct,
                    searchable=control == "search",
                    reason=(
                        "verified in the approved source tables; requires a validated "
                        "new SQL query"
                    ),
                    join_path=relationships[:3],
                )
            )

    def _subtract(present: list[SuggestedFilter], duplicates: list[SuggestedFilter]) -> None:
        seen = {item.field for item in duplicates}
        present[:] = [item for item in present if item.field not in seen][:12]

    _subtract(catalog.sql_filters, catalog.client_filters)
    _subtract(catalog.unavailable_filters, catalog.client_filters)
    _subtract(catalog.unavailable_filters, catalog.sql_filters)
    return catalog
