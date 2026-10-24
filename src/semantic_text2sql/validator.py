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
    required_grain = contract.output_grain or contract.grain
    groups = list(root.find_all(exp.Group))
    if contract.requires_grouping and required_grain and not groups:
        status: Literal["FAIL", "UNRESOLVED"] = (
            "UNRESOLVED" if any(root.find_all(exp.Window)) else "FAIL"
        )
        findings.append(SemanticFinding(
            status=status, code="GROUPING_NOT_FOUND",
            evidence="The contract requires grouped output, but SQL has no GROUP BY clause.",
        ))
    elif contract.requires_grouping and required_grain:
        findings.extend(_grouping_findings(root, required_grain, groups))
    findings.extend(_measure_findings(root, contract, verified_context))
    findings.extend(_filter_findings(root, contract, verified_context))
    findings.extend(_fanout_findings(root, verified_context))
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


def _grouping_findings(
    root: exp.Expression, required_grain: list[str], groups: list[exp.Group]
) -> list[SemanticFinding]:
    aliases = {
        alias.alias.casefold(): alias.this.name.casefold()
        for alias in root.find_all(exp.Alias)
        if isinstance(alias.this, exp.Column)
    }
    actual = {
        column.name.casefold()
        for group in groups for expression in group.expressions
        for column in expression.find_all(exp.Column)
    }
    actual.update(
        expression.name.casefold()
        for group in groups for expression in group.expressions
        if isinstance(expression, exp.Column)
    )
    actual.update(aliases[name] for name in list(actual) if name in aliases)
    for group in groups:
        parent = group.parent
        if not isinstance(parent, exp.Select):
            continue
        for expression in group.expressions:
            if not isinstance(expression, exp.Literal) or not expression.is_int:
                continue
            position = int(expression.this) - 1
            if 0 <= position < len(parent.expressions):
                actual.update(
                    item.name.casefold()
                    for item in parent.expressions[position].find_all(exp.Column)
                )
    missing = [name for name in required_grain if name.split(".")[-1].casefold() not in actual]
    if not missing:
        return [SemanticFinding(
            status="PASS", code="GROUPING_DIMENSIONS_PRESENT",
            evidence="Required grouping columns occur in GROUP BY expressions.",
        )]
    # A CTE/window can transform a dimension before the final grouping. Without
    # lineage proof, do not turn that into a false rejection.
    uncertain = bool(list(root.find_all(exp.CTE)) or list(root.find_all(exp.Window)))
    return [SemanticFinding(
        status="UNRESOLVED" if uncertain else "FAIL",
        code="GROUPING_DIMENSION_MISMATCH",
        evidence=f"Required grouping dimension(s) absent from GROUP BY: {', '.join(missing)}.",
    )]


def _measure_findings(
    root: exp.Expression,
    contract: SemanticContract,
    verified_context: dict[str, object] | None,
) -> list[SemanticFinding]:
    if not contract.aggregation or not contract.measures or not verified_context:
        return []
    tables = verified_context.get("tables")
    if not isinstance(tables, dict):
        return []
    physical = {
        column.casefold()
        for table in tables.values() if isinstance(table, dict)
        for column in table.get("columns", {})
    }
    measures = {name.split(".")[-1].casefold() for name in contract.measures}
    known_measures = measures & physical
    if not known_measures:
        return [SemanticFinding(
            status="UNRESOLVED", code="MEASURE_LINEAGE_UNKNOWN",
            evidence=("Requested measure is symbolic; no verified physical "
                      "column mapping was supplied."),
        )]
    expected: dict[str, type[exp.AggFunc]] = {
        "count": exp.Count, "sum": exp.Sum, "average": exp.Avg,
        "min": exp.Min, "max": exp.Max,
    }
    aggregate_nodes = list(root.find_all(expected[contract.aggregation]))
    if not aggregate_nodes:
        return []  # The aggregation-presence finding already covers this case.
    matched = any(
        column.name.casefold() in known_measures
        for aggregate in aggregate_nodes for column in aggregate.find_all(exp.Column)
    )
    if matched:
        return [SemanticFinding(
            status="PASS", code="AGGREGATED_MEASURE_PRESENT",
            evidence="The requested physical measure occurs inside the required aggregate.",
        )]
    if contract.aggregation == "count" and any(
        isinstance(node.this, exp.Star) for node in aggregate_nodes
    ):
        return [SemanticFinding(
            status="UNRESOLVED", code="COUNT_MEASURE_EQUIVALENCE_UNKNOWN",
            evidence="COUNT(*) may count the intended entity only if the input grain is verified.",
        )]
    uncertain = bool(list(root.find_all(exp.CTE)) or list(root.find_all(exp.Subquery)))
    return [SemanticFinding(
        status="UNRESOLVED" if uncertain else "FAIL", code="AGGREGATED_MEASURE_MISMATCH",
        evidence="Required aggregate does not use the requested physical measure.",
    )]


def _filter_findings(
    root: exp.Expression,
    contract: SemanticContract,
    verified_context: dict[str, object] | None,
) -> list[SemanticFinding]:
    findings: list[SemanticFinding] = []
    aliases = {
        table.alias_or_name.casefold(): table.name.casefold()
        for table in root.find_all(exp.Table)
    }
    context_tables = verified_context.get("tables", {}) if verified_context else {}
    predicates = [
        node for where in root.find_all(exp.Where) for node in where.walk()
        if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In,
                             exp.Between))
    ]
    for required in [*contract.filters, *contract.mandatory_metric_filters]:
        column_name = required.operand.split(".")[-1].casefold()
        required_table = (
            required.operand.split(".", 1)[0].casefold()
            if "." in required.operand else None
        )
        relevant = [
            node for node in predicates
            if any(
                col.name.casefold() == column_name
                and (
                    required_table is None
                    or (col.table and aliases.get(col.table.casefold(), col.table.casefold())
                        == required_table)
                    or (not col.table and set(aliases.values()) == {required_table})
                )
                for col in node.find_all(exp.Column)
            )
        ]
        requested = (
            {str(value).casefold() for value in required.value}
            if isinstance(required.value, list)
            else {str(required.value).casefold()}
        )
        matches = [
            node for node in relevant
            if _operator_name(node) == required.operator
            and requested.issubset({
                str(item.this).casefold() for item in node.find_all(exp.Literal)
            })
        ]
        if matches:
            ambiguous = (
                required_table is None and isinstance(context_tables, dict)
                and sum(
                    column_name in {str(name).casefold() for name in table.get("columns", {})}
                    for table in context_tables.values() if isinstance(table, dict)
                ) > 1
                and any(not col.table for node in matches for col in node.find_all(exp.Column))
            )
            status: Literal["PASS", "UNRESOLVED"] = (
                "UNRESOLVED"
                if ambiguous or any(node.find_ancestor(exp.Or) for node in matches)
                else "PASS"
            )
            findings.append(SemanticFinding(
                status=status, code="FILTER_PREDICATE_MATCH",
                evidence=(f"Found the required {required.operand} predicate "
                          "with its operator and value."),
            ))
        elif relevant:
            findings.append(SemanticFinding(
                status="FAIL", code="REQUIRED_FILTER_PREDICATE_MISMATCH",
                evidence=(f"SQL filters {required.operand}, but not with "
                          "the required operator/value."),
            ))
        elif not (len(requested) == 1 and re.fullmatch(r"\d{4}", next(iter(requested)))):
            findings.append(SemanticFinding(
                status="FAIL", code="REQUIRED_FILTER_VALUE_MISSING",
                evidence=(f"No required predicate for {required.operand} "
                          "appears in SQL WHERE clauses."),
            ))
        else:
            findings.append(SemanticFinding(
                status="UNRESOLVED", code="TEMPORAL_PREDICATE_EQUIVALENCE_UNKNOWN",
                evidence="A year may be represented by date bounds; equivalence is not proven.",
            ))
    return findings


def _operator_name(node: exp.Expression) -> str:
    operators: dict[type[exp.Expression], str] = {
        exp.EQ: "=", exp.NEQ: "!=", exp.GT: ">", exp.GTE: ">=",
        exp.LT: "<", exp.LTE: "<=", exp.In: "IN", exp.Between: "BETWEEN",
    }
    return operators.get(type(node), "")


def _fanout_findings(
    root: exp.Expression, verified_context: dict[str, object] | None
) -> list[SemanticFinding]:
    if not verified_context or not any(root.find_all(exp.Sum, exp.Avg)):
        return []
    relationships = verified_context.get("relationships")
    if not isinstance(relationships, list):
        return []
    cte_names = {cte.alias_or_name.casefold() for cte in root.find_all(exp.CTE)}
    physical = {
        table.name.casefold() for table in root.find_all(exp.Table)
        if table.name.casefold() not in cte_names
    }
    for relationship in relationships:
        if not isinstance(relationship, dict):
            continue
        left = str(relationship.get("left") or "").split(".")[0].casefold()
        right = str(relationship.get("right") or "").split(".")[0].casefold()
        if (left in physical and right in physical and left != right
                and relationship.get("cardinality") == "MANY_TO_MANY"):
            return [SemanticFinding(
                status="UNRESOLVED",
                code="FACT_JOIN_FANOUT_RISK",
                evidence=(f"Aggregating after a many-to-many join between "
                          f"{left} and {right} may multiply measures; a data-backed "
                          "cardinality check is needed before rejection."),
            )]
    return []


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
