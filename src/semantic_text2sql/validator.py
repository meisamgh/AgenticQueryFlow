"""Minimal fail-closed validation for SQL syntax and read-only safety."""

from __future__ import annotations

import re
from typing import Literal

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from semantic_text2sql.models import (
    SchemaInfo,
    SemanticContract,
    SemanticFinding,
    ValidationResult,
)

_FENCE = re.compile(r"^```(?:sql)?\s*(.*?)\s*```$", re.IGNORECASE | re.DOTALL)
_EMBEDDED_SQL_FENCE = re.compile(r"```sql\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)
_FORBIDDEN = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.Command,
    exp.Merge,
    exp.Lock,
)


def clean_model_sql(value: str) -> str:
    stripped = value.strip()
    match = _FENCE.fullmatch(stripped)
    if match:
        stripped = match.group(1)
    else:
        embedded = _EMBEDDED_SQL_FENCE.search(stripped)
        if embedded:
            stripped = embedded.group(1)
    return stripped.strip().removesuffix(";").strip()


def normalize_sql(value: str, *, dialect: Literal["sqlite", "postgres"] = "sqlite") -> str:
    try:
        return sqlglot.parse_one(value, read=dialect).sql(dialect=dialect, pretty=False)
    except (ParseError, TokenError):
        return " ".join(value.split())


def validate_sql(
    sql: str,
    schema: SchemaInfo,
    *,
    dialect: Literal["sqlite", "postgres"] = "sqlite",
    allowed_tables: set[str] | None = None,
    allowed_columns: dict[str, set[str]] | None = None,
) -> ValidationResult:
    available = {table.name: [column.name for column in table.columns] for table in schema.tables}
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except (ParseError, TokenError) as exc:
        return _failure("SQL_PARSE_FAILED", f"Generated SQL could not be parsed: {exc}", available)
    if len(statements) != 1:
        return _failure("SQL_MULTIPLE_STATEMENTS", "Exactly one statement is allowed.", available)
    root = statements[0]
    if root is None or not isinstance(root, exp.Query):
        return _failure("SQL_NOT_READ_ONLY", "Only SELECT or WITH...SELECT is allowed.", available)
    if any(isinstance(node, _FORBIDDEN) for node in root.walk()):
        return _failure(
            "SQL_NOT_READ_ONLY",
            "Write and administrative operations are blocked.",
            available,
        )
    if any(isinstance(node, exp.Into) for node in root.walk()):
        return _failure("SQL_SELECT_INTO_BLOCKED", "SELECT INTO is blocked.", available)

    cte_names = {cte.alias_or_name.casefold() for cte in root.find_all(exp.CTE)}
    physical_tables = [
        table.name
        for table in root.find_all(exp.Table)
        if table.name and table.name.casefold() not in cte_names
    ]
    if allowed_tables is not None:
        unauthorized = sorted(
            {table for table in physical_tables if table.casefold() not in allowed_tables}
        )
        if unauthorized:
            return _failure(
                "SQL_TABLE_NOT_AUTHORIZED",
                "SQL references tables outside the authorized context: " + ", ".join(unauthorized),
                available,
                tables=physical_tables,
            )
    columns = list(
        dict.fromkeys(column.sql(dialect=dialect) for column in root.find_all(exp.Column))
    )
    if allowed_columns is not None:
        normalized_columns = {
            table.casefold(): {column.casefold() for column in names}
            for table, names in allowed_columns.items()
        }
        aliases = {
            (table.alias_or_name or table.name).casefold(): table.name.casefold()
            for table in root.find_all(exp.Table)
            if table.name and table.name.casefold() not in cte_names
        }
        select_aliases = {
            alias.alias.casefold() for alias in root.find_all(exp.Alias) if alias.alias
        }
        unauthorized_columns: list[str] = []
        for column in root.find_all(exp.Column):
            if not column.name or column.table.casefold() in cte_names:
                continue
            if column.table:
                owner = aliases.get(column.table.casefold(), column.table.casefold())
                allowed = normalized_columns.get(owner)
                if allowed is not None and column.name.casefold() not in allowed:
                    unauthorized_columns.append(column.sql(dialect=dialect))
            elif column.name.casefold() not in select_aliases and not any(
                column.name.casefold() in names for names in normalized_columns.values()
            ):
                unauthorized_columns.append(column.sql(dialect=dialect))
        if unauthorized_columns:
            return _failure(
                "SQL_COLUMN_NOT_AUTHORIZED",
                "SQL references columns outside the authorized context: "
                + ", ".join(sorted(set(unauthorized_columns))),
                available,
                tables=physical_tables,
            )
    return ValidationResult(
        valid=True,
        code="SQL_SAFETY_VALID",
        message="SQL is one syntactically valid read-only statement.",
        tables=list(dict.fromkeys(physical_tables)),
        columns=columns,
        available_columns=available,
    )


def validate_sql_semantics(
    sql: str,
    contract: SemanticContract,
    verified_context: dict[str, object] | None = None,
    *,
    dialect: Literal["sqlite", "postgres"] = "sqlite",
) -> list[SemanticFinding]:
    """Reject only contract mismatches provable from the SQL AST.

    Unverified business meaning, equivalence of alternate formulas, and join
    fanout require data-backed review and are explicitly marked UNRESOLVED.
    The caller must run ``validate_sql`` first; this function never executes SQL.
    """
    del verified_context  # Reserved for verified lineage/cardinality checks.
    root = sqlglot.parse_one(sql, read=dialect)
    findings: list[SemanticFinding] = []
    aliases = {
        alias.alias.casefold() for alias in root.find_all(exp.Alias) if alias.alias
    }
    for required in contract.required_output_aliases:
        if required.casefold() not in aliases:
            findings.append(SemanticFinding(
                status="FAIL", code="REQUIRED_OUTPUT_MISSING",
                evidence=f"Required output alias {required!r} is absent from the SELECT list.",
            ))
        else:
            findings.append(SemanticFinding(
                status="PASS", code="REQUIRED_OUTPUT_PRESENT",
                evidence=f"Required output alias {required!r} is present.",
            ))
    aggregates = {type(node) for node in root.find_all(exp.AggFunc)}
    expected: dict[str, type[exp.AggFunc]] = {
        "count": exp.Count, "sum": exp.Sum, "average": exp.Avg,
        "min": exp.Min, "max": exp.Max,
    }
    if contract.aggregation and expected[contract.aggregation] not in aggregates:
        # SUM(x)/COUNT(x) and similar rewrites can implement AVG without AVG().
        # They need stronger lineage proof rather than a structural rejection.
        alternate = contract.aggregation == "average" and bool(
            list(root.find_all(exp.Div))
        )
        findings.append(SemanticFinding(
            status="UNRESOLVED" if alternate else "FAIL",
            code="AGGREGATION_NOT_FOUND",
            evidence=f"The SQL has no {contract.aggregation.upper()} aggregation node.",
        ))
    elif contract.aggregation:
        findings.append(SemanticFinding(
            status="PASS", code="AGGREGATION_PRESENT",
            evidence=f"Found a {contract.aggregation.upper()} aggregation node.",
        ))
    if contract.requires_grouping and contract.grain and not any(root.find_all(exp.Group)):
        status: Literal["FAIL", "UNRESOLVED"] = (
            "UNRESOLVED" if any(root.find_all(exp.Window)) else "FAIL"
        )
        findings.append(SemanticFinding(
            status=status, code="GROUPING_NOT_FOUND",
            evidence="The contract requires grouped output, but SQL has no GROUP BY clause.",
        ))
    elif contract.requires_grouping and contract.grain:
        findings.append(SemanticFinding(
            status="PASS", code="GROUPING_PRESENT",
            evidence="SQL contains a GROUP BY clause; exact output grain remains unverified.",
        ))
    literals = {
        str(node.this).casefold() for node in root.find_all(exp.Literal)
    }
    for requirement in [*contract.filters, *contract.mandatory_metric_filters]:
        value = requirement.value
        required_values = [str(item) for item in value] if isinstance(value, list) else [str(value)]
        # Only exact scalar categorical requirements are provable here. A year
        # might be represented as bounds on a YYYYMM date and is not rejected.
        for item in required_values:
            if item.casefold() not in literals and not re.fullmatch(r"\d{4}", item):
                findings.append(SemanticFinding(
                    status="FAIL", code="REQUIRED_FILTER_VALUE_MISSING",
                    evidence=(
                        f"Required filter value {item!r} for {requirement.operand!r} "
                        "does not occur in SQL literals."
                    ),
                ))
            elif item.casefold() in literals:
                findings.append(SemanticFinding(
                    status="UNRESOLVED", code="FILTER_PREDICATE_UNVERIFIED",
                    evidence=(f"Required value {item!r} occurs in SQL literals, but this "
                              "alone does not prove the intended filter predicate."),
                ))
    if contract.requires_distinct and not any(root.find_all(exp.Distinct)):
        findings.append(SemanticFinding(
            status="UNRESOLVED", code="DISTINCT_NOT_OBVIOUS",
            evidence="No DISTINCT node was found; grouping or keys might still provide uniqueness.",
        ))
    if contract.approved_metrics:
        findings.append(SemanticFinding(
            status="UNRESOLVED", code="METRIC_FORMULA_EQUIVALENCE_UNPROVEN",
            evidence="Approved metric formula equivalence needs lineage-aware validation.",
        ))
    return findings


def repair_context(result: ValidationResult) -> str:
    guidance = ""
    if (
        result.code == "DATABASE_ERROR"
        and "ORDER BY clause should come after" in result.message
        and "not before" in result.message
    ):
        guidance = (
            "\nRequired structural repair: SQLite does not allow branch-level ORDER BY/LIMIT "
            "directly before a compound operator. Wrap each ranked branch in its own subquery "
            "or CTE, or replace the compound query with window-function ranking. Do not repeat "
            "the rejected compound-query structure."
        )
    return (f"Validation code: {result.code}\nValidation message: {result.message}{guidance}")[
        :12_000
    ]


def _failure(
    code: str,
    message: str,
    available: dict[str, list[str]],
    *,
    tables: list[str] | None = None,
) -> ValidationResult:
    return ValidationResult(
        valid=False,
        code=code,
        message=message,
        tables=tables or [],
        available_columns=available,
    )
