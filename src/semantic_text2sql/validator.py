"""Minimal fail-closed validation for SQL syntax and read-only safety."""

from __future__ import annotations

import re
from typing import Literal

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError, TokenError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import Scope, traverse_scope

from semantic_text2sql.dialects import DatabaseDialect
from semantic_text2sql.models import (
    SchemaInfo,
    SemanticContract,
    SemanticFinding,
    ValidationResult,
)
from semantic_text2sql.semantic_lineage import ExpressionLineage, expression_key

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


def normalize_sql(value: str, *, dialect: DatabaseDialect = "sqlite") -> str:
    try:
        return sqlglot.parse_one(value, read=dialect).sql(dialect=dialect, pretty=False)
    except (ParseError, TokenError):
        return " ".join(value.split())


def validate_sql(
    sql: str,
    schema: SchemaInfo,
    *,
    dialect: DatabaseDialect = "sqlite",
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

    try:
        physical_refs = physical_table_references(root, dialect=dialect)
    except (OptimizeError, ValueError) as exc:
        return _failure(
            "SQL_SCOPE_UNRESOLVED", f"SQL scopes could not be resolved: {exc}", available
        )
    physical_tables = [table.name for table in physical_refs if table.name]
    if allowed_tables is not None:
        authorized = {name.casefold() for name in allowed_tables}
        unauthorized = sorted(
            {table.sql(dialect=dialect) for table in physical_refs
             if not table_is_allowlisted(table, authorized)}
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
            for table in physical_refs if table.name
        }
        derived_columns = {
            id(column) for scope in traverse_scope(root) for column in scope.columns
            if column.table and not column.db and not column.catalog
            and isinstance(scope.sources.get(column.table), Scope)
        }
        select_aliases = {
            alias.alias.casefold() for alias in root.find_all(exp.Alias) if alias.alias
        }
        unauthorized_columns: list[str] = []
        for column in root.find_all(exp.Column):
            if not column.name or id(column) in derived_columns:
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


def physical_table_references(
    root: exp.Expression, *, dialect: DatabaseDialect = "sqlite",
) -> list[exp.Table]:
    """Actual AST table nodes, excluding only lexically visible unqualified CTEs.

    Resolve a copy using engine case rules (including quoted names), returning
    original nodes without rewriting SQL. Qualified identifiers can never bind
    a CTE. All table nodes include SEMI/ANTI joins, which selected_sources omits.
    """
    resolved = normalize_identifiers(root.copy(), dialect=dialect)
    original_tables = list(root.find_all(exp.Table))
    resolved_tables = list(resolved.find_all(exp.Table))
    if len(original_tables) != len(resolved_tables):
        raise ValueError("Identifier normalization changed the table-reference structure")
    cte_references = {
        id(table) for scope in traverse_scope(resolved) for table in scope.tables
        if not table.db and not table.catalog
        and isinstance(table.this, exp.Identifier)
        and isinstance(scope.cte_sources.get(table.name), Scope)
    }
    return [original for original, table in zip(original_tables, resolved_tables, strict=True)
            if id(table) not in cte_references]


def table_is_allowlisted(table: exp.Table, allowed: set[str]) -> bool:
    """Bare allowlist entries authorize names; qualified entries require identity.

    Engine adapters must independently restrict schemas/catalogs when their
    configuration uses bare table names.
    """
    full_name = ".".join(part.name.casefold() for part in table.parts)
    return table.name.casefold() in allowed or full_name in allowed


def validate_sql_semantics(
    sql: str,
    contract: SemanticContract,
    verified_context: dict[str, object] | None = None,
    *,
    dialect: DatabaseDialect = "sqlite",
) -> list[SemanticFinding]:
    """Reject only contract mismatches provable from the SQL AST.

    Unverified business meaning, equivalence of alternate formulas, and join
    fanout require data-backed review and are explicitly marked UNRESOLVED.
    The caller must run ``validate_sql`` first; this function never executes SQL.
    """
    root = sqlglot.parse_one(sql, read=dialect)
    findings: list[SemanticFinding] = []
    aliases = {item.alias_or_name.casefold() for item in root.selects
               if item.alias_or_name} if isinstance(root, exp.Query) else set()
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
    output_lineage = ExpressionLineage(root, verified_context).output_expressions()
    aggregates = {type(node) for output in output_lineage if output is not None
                  for node in output.find_all(exp.AggFunc)}
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
            status="UNRESOLVED" if alternate or not output_lineage
            or any(item is None for item in output_lineage) else "FAIL",
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
        findings.extend(_grouping_findings(root, required_grain, groups, verified_context,
                                           strict_output_grain=bool(contract.output_grain)))
    findings.extend(_measure_findings(root, contract, verified_context))
    findings.extend(_approved_metric_findings(root, contract, dialect))
    findings.extend(_filter_findings(root, contract, verified_context))
    findings.extend(_fanout_findings(root, verified_context, dialect=dialect))
    findings.extend(_ranking_findings(root, contract))
    source_names = [".".join(part.name for part in table.parts)
                    for table in physical_table_references(root, dialect=dialect)]
    if len(source_names) != len(set(source_names)):
        findings.append(SemanticFinding(
            status="UNRESOLVED", code="REPEATED_SOURCE_ROLE_UNPROVEN",
            evidence="Repeated physical sources need instance/role lineage beyond table identity.",
        ))
    if contract.active_period and not contract.temporal_boundaries:
        findings.append(SemanticFinding(
            status="UNRESOLVED", code="ACTIVE_PERIOD_EQUIVALENCE_UNPROVEN",
            evidence="The symbolic active period has no explicit boundaries proving its SQL scope.",
        ))
    if contract.requires_distinct:
        final_distinct = bool(root.args.get("distinct"))
        counted_distinct = contract.aggregation == "count" and any(
            isinstance(node.this, exp.Distinct) for output in output_lineage if output is not None
            for node in output.find_all(exp.Count))
        findings.append(SemanticFinding(
            status="PASS" if final_distinct or counted_distinct else "UNRESOLVED",
            code=("DISTINCT_PRESENT" if final_distinct or counted_distinct
                  else "DISTINCT_NOT_OBVIOUS"),
            evidence="Checked DISTINCT in the final output lineage, not unrelated CTEs.",
        ))
    return findings


def _approved_metric_findings(
    root: exp.Expression, contract: SemanticContract,
    dialect: DatabaseDialect,
) -> list[SemanticFinding]:
    findings: list[SemanticFinding] = []
    complex_lineage = bool(list(root.find_all(exp.CTE)) or list(root.find_all(exp.Subquery)))
    for metric in contract.approved_metrics:
        try:
            formula = sqlglot.parse_one(metric.formula, read=dialect)
        except (ParseError, TokenError):
            formula = None
        if formula is not None and not isinstance(formula, exp.AggFunc):
            expected_expression = formula.transform(
                lambda node, source=metric.source_relation: exp.column(
                    node.name.casefold(), table=source.casefold())
                if isinstance(node, exp.Column) else node
            )
            outputs = ExpressionLineage(root).output_expressions()
            if any(item is not None and expression_key(item) == expression_key(expected_expression)
                   for item in outputs):
                findings.append(SemanticFinding(
                    status="PASS", code="APPROVED_METRIC_FORMULA_MATCH",
                    evidence=f"The output implements the approved {metric.id} expression.",
                ))
                continue
        if not isinstance(formula, exp.AggFunc) or not isinstance(formula.this, exp.Column):
            findings.append(SemanticFinding(
                status="UNRESOLVED", code="METRIC_FORMULA_EQUIVALENCE_UNPROVEN",
                evidence=f"The approved {metric.id} formula needs derived-expression lineage.",
            ))
            continue
        required_column = formula.this.name.casefold()
        candidates = [
            node for node in root.find_all(type(formula))
            if isinstance(node.this, exp.Column)
            and node.this.name.casefold() == required_column
        ]
        if candidates:
            expected_expression = formula.transform(
                lambda node, source=metric.source_relation: exp.column(
                    node.name.casefold(), table=source.casefold())
                if isinstance(node, exp.Column) else node)
            outputs = ExpressionLineage(root).output_expressions()
            proven = (len([node for item in outputs if item is not None
                           for node in item.find_all(exp.AggFunc)]) == 1
                      and any(item is not None and expression_key(item)
                              == expression_key(expected_expression) for item in outputs))
            findings.append(SemanticFinding(
                status="PASS" if proven else "UNRESOLVED",
                code="APPROVED_METRIC_INPUT_MATCH" if proven
                else "METRIC_FORMULA_EQUIVALENCE_UNPROVEN",
                evidence=(f"The {metric.id} aggregate uses its approved input."
                          if proven else f"The {metric.id} input matches by name, but "
                          "its source relation is not proven."),
            ))
        else:
            projection = (root.expressions[0].unalias()
                          if isinstance(root, exp.Select) and len(root.expressions) == 1 else None)
            wrong_direct_function = (
                not complex_lineage and isinstance(projection, exp.AggFunc)
                and type(projection) is not type(formula)
                and isinstance(projection.this, exp.Column)
                and projection.this.name.casefold() == required_column
            )
            direct_wrong_input = bool(list(root.find_all(type(formula)))) and all(
                isinstance(node.this, exp.Column)
                for node in root.find_all(type(formula))
            )
            findings.append(SemanticFinding(
                status="FAIL" if wrong_direct_function or (
                    direct_wrong_input and not complex_lineage) else "UNRESOLVED",
                code="APPROVED_METRIC_INPUT_MISMATCH",
                evidence=f"No direct {type(formula).__name__.upper()} on "
                         f"{metric.source_relation}.{required_column} implements {metric.id}.",
            ))
    return findings


def _grouping_findings(
    root: exp.Expression, required_grain: list[str], groups: list[exp.Group],
    verified_context: dict[str, object] | None = None,
    *, strict_output_grain: bool = False,
) -> list[SemanticFinding]:
    lineage = ExpressionLineage(root)
    group = root.args.get("group")
    if not isinstance(group, exp.Group):
        return [SemanticFinding(status="UNRESOLVED", code="GROUPING_DIMENSION_MISMATCH",
                                evidence="Output grain depends on an intermediate query.")]
    actual: list[exp.Expression | None] = []
    for item in group.expressions:
        if isinstance(item, exp.Literal) and item.is_int:
            index = int(item.this) - 1
            item = root.expressions[index] if 0 <= index < len(root.expressions) else item
        actual.append(lineage.resolve(item, aliases=True))
    uncertain = any(item is None for item in actual)
    covered_keys: set[int] = set()
    for grain in required_grain:
        try:
            expected = sqlglot.parse_one(grain)
        except ParseError:
            expected = None
        if expected is None:
            uncertain = True
            continue
        tables = verified_context.get("tables", {}) if verified_context else {}
        physical_names = ({str(column).casefold()
                          for table in tables.values() if isinstance(table, dict)
                          for column in table.get("columns", {})}
                          if isinstance(tables, dict) else set())
        if (isinstance(expected, exp.Column) and not expected.table
                and expected.name.casefold() not in physical_names
                and expected.name.casefold() in {
                    item.alias.casefold() for item in root.expressions if item.alias}):
            resolved = lineage.resolve(expected, aliases=True)
            if verified_context and resolved is not None:
                expected = resolved
            else:
                uncertain = True
        qualified = any(column.table for column in expected.find_all(exp.Column))
        matched_keys = [index for index, item in enumerate(actual) if item is not None
                        and expression_key(item, qualified=qualified)
                        == expression_key(expected, qualified=qualified)]
        covered_keys.update(matched_keys)
        if not qualified and len({expression_key(item) for index, item in enumerate(actual)
                                  if index in matched_keys and item is not None}) > 1:
            uncertain = True
        if not matched_keys:
            return [SemanticFinding(
                status="UNRESOLVED" if uncertain else "FAIL",
                code="GROUPING_DIMENSION_MISMATCH",
                evidence="The final grouping expression does not establish the required grain.",
            )]
    if strict_output_grain and len(covered_keys) != len(actual):
        return [SemanticFinding(
            status="UNRESOLVED", code="FINAL_GRAIN_EQUIVALENCE_UNPROVEN",
            evidence="Additional grouping keys need functional-dependency proof; "
                     "the requested output grain is not verified.",
        )]
    return [SemanticFinding(
        status="UNRESOLVED" if uncertain else "PASS", code="GROUPING_DIMENSIONS_PRESENT",
        evidence="Required final grouping expressions were checked through scoped lineage.",
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
    outputs = ExpressionLineage(root, verified_context).output_expressions()
    aggregate_nodes = [node for output in outputs if output is not None
                       for node in output.find_all(expected[contract.aggregation])]
    if not aggregate_nodes:
        return [SemanticFinding(status="UNRESOLVED", code="MEASURE_LINEAGE_UNKNOWN",
                                evidence="Output aggregate lineage is not established.")]
    matched = all(any(
        isinstance(column, exp.Column)
        and column.name.casefold() == measure.split(".")[-1].casefold()
        and ("." not in measure or column.table.casefold()
             == measure.rsplit(".", 1)[0].casefold())
        for aggregate in aggregate_nodes
        for column in (aggregate.this.expressions if isinstance(aggregate.this, exp.Distinct)
                       else [aggregate.this])
    ) for measure in contract.measures)
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
    uncertain = (any(output is None for output in outputs)
                 or any(not isinstance(node.this, exp.Column) for node in aggregate_nodes))
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
    if not isinstance(root, exp.Select):
        return [SemanticFinding(status="UNRESOLVED", code="FILTER_PREDICATE_UNPROVEN",
                                evidence="Set-operation predicates require branch-wise proof.")]
    lineage = ExpressionLineage(root, verified_context)
    context_tables = verified_context.get("tables", {}) if verified_context else {}
    predicates = []
    for scope in lineage.reachable():
        where = scope.expression.args.get("where")
        if not isinstance(where, exp.Where):
            continue
        for node in where.walk():
            if (isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE,
                                  exp.In, exp.Between))
                    and node.find_ancestor(exp.Select) is scope.expression):
                predicates.append((node, scope))
    for required, temporal in [
        *((item, False) for item in contract.filters),
        *((item, False) for item in contract.mandatory_metric_filters),
        *((item, True) for item in contract.temporal_boundaries),
    ]:
        column_name = required.operand.split(".")[-1].casefold()
        required_table = (
            required.operand.rsplit(".", 1)[0].casefold()
            if "." in required.operand else None
        )
        relevant = []
        matches: list[exp.Expression] = []
        unknown_operand = False
        bounds: dict[tuple[int, str], list[exp.Expression]] = {}
        requested_values = required.value if isinstance(required.value, list) else [required.value]
        requested = [str(value) for value in requested_values]
        for node, scope in predicates:
            operand, operator, values = node.this, _operator_name(node), []
            if isinstance(node, exp.Between):
                values = [node.args.get("low"), node.args.get("high")]
            elif isinstance(node, exp.In):
                values = list(node.expressions)
            elif isinstance(node.expression, exp.Literal):
                values = [node.expression]
            elif isinstance(node.this, exp.Literal) and isinstance(node.expression, exp.Column):
                operand, values = node.expression, [node.this]
                operator = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}.get(operator, operator)
            resolved = lineage.resolve(operand, scope)
            if not isinstance(resolved, exp.Column):
                unknown_operand |= any(col.name.casefold() == column_name
                                       for col in operand.find_all(exp.Column))
                continue  # A transformed operand cannot establish a physical filter.
            if resolved.name.casefold() != column_name or (
                required_table and resolved.table.casefold() != required_table
            ):
                continue
            relevant.append(node)
            if not all(isinstance(value, exp.Literal) for value in values) or not values:
                unknown_operand = True
                continue
            observed = [str(value.this) for value in values if isinstance(value, exp.Literal)]
            if (observed != requested and [value.casefold() for value in observed]
                    == [value.casefold() for value in requested]):
                unknown_operand = True  # Collation/canonical-value proof is not in this context.
            if not node.find_ancestor(exp.Or, exp.Not) and len(requested) == 2:
                if operator == ">=" and observed == [requested[0]]:
                    bounds.setdefault((id(scope), "lower"), []).append(node)
                if operator == "<=" and observed == [requested[1]]:
                    bounds.setdefault((id(scope), "upper"), []).append(node)
            equal = observed == requested
            if operator == required.operator == "IN":
                equal = set(observed) == set(requested)
            equivalent_operator = operator == required.operator or (
                {operator, required.operator} == {"=", "IN"}
                and len(observed) == len(requested) == 1
            )
            if equivalent_operator and equal:
                matches.append(node)
        if required.operator == "BETWEEN":
            for scope in lineage.reachable():
                lower, upper = bounds.get((id(scope), "lower")), bounds.get((id(scope), "upper"))
                if lower and upper:
                    matches.extend([*lower, *upper])
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
                if ambiguous or any(node.find_ancestor(exp.Or, exp.Not) for node in matches)
                or (any(root.find_all(exp.Join))
                    and any(node.find_ancestor(exp.Select) is not root for node in matches))
                else "PASS"
            )
            findings.append(SemanticFinding(
                status=status, code="FILTER_PREDICATE_MATCH",
                evidence=(f"Found the required {required.operand} predicate "
                          "with its operator and value."),
            ))
        elif relevant:
            uncertain = unknown_operand or temporal and bool(
                list(root.find_all(exp.CTE)) or list(root.find_all(exp.Subquery))
            )
            findings.append(SemanticFinding(
                status="UNRESOLVED" if uncertain else "FAIL",
                code="REQUIRED_FILTER_PREDICATE_MISMATCH",
                evidence=(f"SQL filters {required.operand}, but not with "
                          "the required operator/value."),
            ))
        elif not (len(requested) == 1 and re.fullmatch(r"\d{4}", requested[0])):
            uncertain = unknown_operand or temporal and bool(
                list(root.find_all(exp.CTE)) or list(root.find_all(exp.Subquery))
            )
            findings.append(SemanticFinding(
                status="UNRESOLVED" if uncertain else "FAIL",
                code="REQUIRED_FILTER_VALUE_MISSING",
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
    root: exp.Expression, verified_context: dict[str, object] | None,
    *, dialect: DatabaseDialect = "sqlite",
) -> list[SemanticFinding]:
    if not verified_context or not any(root.find_all(exp.Sum, exp.Avg, exp.Count)):
        return []
    relationships = verified_context.get("relationships")
    if not isinstance(relationships, list):
        return []
    # Prove multiplicity only for a single conjunctive equijoin. More complex
    # populations/join chains remain UNRESOLVED rather than guessed safe.
    lineage = ExpressionLineage(root, verified_context)
    joins = root.args.get("joins", [])
    if len(joins) == 1 and joins[0].args.get("on") is not None:
        on = joins[0].args["on"]
        if not on.find(exp.Or, exp.Not):
            actual = set()
            for equality in on.find_all(exp.EQ):
                a, b = lineage.resolve(equality.this), lineage.resolve(equality.expression)
                if isinstance(a, exp.Column) and isinstance(b, exp.Column):
                    actual.add(frozenset((f"{a.table}.{a.name}", f"{b.table}.{b.name}")))
            for relation in relationships:
                if not isinstance(relation, dict):
                    continue
                left_keys = relation.get("left_columns") or [relation.get("left")]
                right_keys = relation.get("right_columns") or [relation.get("right")]
                if not left_keys or len(left_keys) != len(right_keys):
                    continue
                expected = {frozenset((str(a).casefold(), str(b).casefold()))
                            for a, b in zip(left_keys, right_keys, strict=True)}
                if not expected <= actual:
                    continue
                left = str(left_keys[0]).rsplit(".", 1)[0].casefold()
                right = str(right_keys[0]).rsplit(".", 1)[0].casefold()
                unique = {left: relation.get("left_key_unique") is True,
                          right: relation.get("right_key_unique") is True}
                table_facts = verified_context.get("tables", {})
                primary_keys = {}
                for table, metadata in table_facts.items() if isinstance(table_facts, dict) else []:
                    if isinstance(metadata, dict):
                        primary_keys[str(table).casefold()] = {
                            f"{table}.{key}".casefold() for key in metadata.get("primary_key", [])}
                # Observed profile uniqueness alone is not a durable key guarantee.
                unique[left] &= set(map(str.casefold, left_keys)) == primary_keys.get(left, set())
                unique[right] &= (set(map(str.casefold, right_keys))
                                  == primary_keys.get(right, set()))
                if lineage.root:
                    for _, source in lineage.root.selected_sources.values():
                        if not isinstance(source, Scope):
                            continue
                        group = source.expression.args.get("group")
                        if isinstance(group, exp.Group):
                            columns = [lineage.resolve(item, source) for item in group.expressions]
                            if columns and all(isinstance(item, exp.Column) for item in columns):
                                keys = {f"{item.table}.{item.name}" for item in columns
                                        if isinstance(item, exp.Column)}
                                if keys == {str(key).casefold() for key in left_keys}:
                                    unique[left] = True
                                if keys == {str(key).casefold() for key in right_keys}:
                                    unique[right] = True
                outputs = lineage.output_expressions()
                aggregate_nodes = [node for item in outputs if item is not None
                                   for node in item.find_all(exp.AggFunc)]
                if (len(aggregate_nodes) == 1 and isinstance(aggregate_nodes[0], exp.Count)
                        and isinstance(aggregate_nodes[0].this, exp.Distinct)):
                    columns = aggregate_nodes[0].this.expressions
                    counted_keys = {f"{item.table}.{item.name}" for item in columns
                                    if isinstance(item, exp.Column)}
                    if (len(counted_keys) == len(columns)
                            and counted_keys in [primary_keys.get(left), primary_keys.get(right)]):
                        return [SemanticFinding(status="PASS",
                                                code="DISTINCT_ENTITY_MULTIPLICITY_PRESERVED",
                                                evidence="Distinct counting covers the full entity "
                                                "primary key across the verified join.")]
                inputs = {column.table for item in outputs if item is not None
                          for aggregate in item.find_all(exp.Sum, exp.Avg, exp.Count)
                          for column in aggregate.find_all(exp.Column)}
                if (inputs and all(item is not None for item in outputs)
                        and inputs <= {left, right}
                        and all(unique[right if source == left else left] for source in inputs)):
                    return [SemanticFinding(status="PASS", code="JOIN_MULTIPLICITY_PRESERVED",
                                            evidence="Complete join keys and verified uniqueness "
                                            "preserve the aggregate input multiplicity.")]
    physical = {
        table.name.casefold() for table in physical_table_references(root, dialect=dialect)
    }
    for relationship in relationships:
        if not isinstance(relationship, dict):
            continue
        left = str(relationship.get("left") or "").split(".")[0].casefold()
        right = str(relationship.get("right") or "").split(".")[0].casefold()
        if (left in physical and right in physical and left != right
                and (relationship.get("cardinality") == "MANY_TO_MANY"
                     or relationship.get("fanout_risk") is True)):
            return [SemanticFinding(
                status="UNRESOLVED",
                code="FACT_JOIN_FANOUT_RISK",
                evidence=(f"Aggregating across {left} and {right} may multiply measures; "
                          "the measure grain and join keys need a data-backed check."),
            )]
    return []


def _ranking_findings(root: exp.Expression, contract: SemanticContract) -> list[SemanticFinding]:
    if (not contract.requires_ordering and not contract.ranking_direction
            and contract.limit is None and not contract.ranked_metric):
        return []
    findings: list[SemanticFinding] = []
    outer_order = root.args.get("order")
    complex_ranking = bool(list(root.find_all(exp.Window)) or list(root.find_all(exp.CTE)))
    if contract.requires_ordering and not isinstance(outer_order, exp.Order):
        findings.append(SemanticFinding(
            status="UNRESOLVED" if complex_ranking else "FAIL",
            code="RANKING_ORDER_MISSING",
            evidence="No final ORDER BY is present; an intermediate ranking may still select rows.",
        ))
    if contract.ranking_direction and isinstance(outer_order, exp.Order):
        first = outer_order.expressions[0] if outer_order.expressions else None
        if isinstance(first, exp.Ordered):
            actual_direction = "DESC" if first.args.get("desc") else "ASC"
            findings.append(SemanticFinding(
                status="PASS" if actual_direction == contract.ranking_direction else "FAIL",
                code="RANKING_DIRECTION_MATCH" if actual_direction == contract.ranking_direction
                else "RANKING_DIRECTION_MISMATCH",
                evidence=f"The first final ORDER BY key is {actual_direction}; expected "
                         f"{contract.ranking_direction}.",
            ))
    if contract.ranked_metric:
        first = (
            outer_order.expressions[0]
            if isinstance(outer_order, exp.Order) and outer_order.expressions else None
        )
        key = first.this if isinstance(first, exp.Ordered) else None
        expected_name = re.sub(
            r"[\s_-]+", "", contract.ranked_metric.split(".")[-1].casefold()
        )
        actual_name = key.name.casefold() if isinstance(key, exp.Column) else None
        if actual_name is not None:
            actual_name = re.sub(r"[\s_-]+", "", actual_name)
        if actual_name == expected_name:
            findings.append(SemanticFinding(
                status="PASS", code="RANKED_METRIC_MATCH",
                evidence=f"The final ORDER BY uses {contract.ranked_metric}.",
            ))
        else:
            projected_names = {
                re.sub(r"[\s_-]+", "", expression.alias_or_name.casefold())
                for expression in root.expressions if expression.alias_or_name
            }
            uncertain = (
                complex_ranking or not isinstance(key, exp.Column)
                or expected_name not in projected_names
            )
            findings.append(SemanticFinding(
                status="UNRESOLVED" if uncertain else "FAIL",
                code="RANKED_METRIC_UNPROVEN" if uncertain else "RANKED_METRIC_MISMATCH",
                evidence=f"The final ORDER BY does not directly use {contract.ranked_metric}.",
            ))
    outer_limit = root.args.get("limit")
    if contract.limit is not None:
        actual_limit = outer_limit.expression if isinstance(outer_limit, exp.Limit) else None
        if isinstance(actual_limit, exp.Literal) and actual_limit.is_int:
            actual_count = int(actual_limit.this)
            findings.append(SemanticFinding(
                status="PASS" if actual_count == contract.limit else "FAIL",
                code="RANKING_LIMIT_MATCH" if actual_count == contract.limit
                else "RANKING_LIMIT_MISMATCH",
                evidence=f"The final LIMIT is {actual_count}; expected {contract.limit}.",
            ))
        else:
            findings.append(SemanticFinding(
                status="UNRESOLVED" if complex_ranking else "FAIL",
                code="RANKING_LIMIT_MISSING",
                evidence="No literal final LIMIT implements the requested rank count.",
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
