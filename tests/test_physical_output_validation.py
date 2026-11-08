"""Selected/filtering columns do not prove final projected output columns."""
import asyncio
from typing import Any

import pytest
from test_agent import FakeModel

from semantic_text2sql.agent import TextToSQLAgent
from semantic_text2sql.models import GenerateRequest, SemanticContract
from semantic_text2sql.validator import validate_sql_semantics

CONTEXT = {"tables": {
    "orders": {"columns": {"amount": {}, "department_id": {}}},
    "departments": {"columns": {"id": {}, "name": {}}},
}}


@pytest.mark.parametrize("sql", [
    "SELECT SUM(amount) AS total FROM orders",
    "SELECT SUM(o.amount) AS total FROM orders o JOIN departments d ON o.department_id=d.id "
    "WHERE d.name='A'",
    "SELECT SUM(o.amount) FROM orders o JOIN departments d ON o.department_id=d.id GROUP BY d.name",
    "WITH unused AS (SELECT name FROM departments) SELECT SUM(amount) FROM orders",
    "SELECT 'A' AS name, SUM(amount) FROM orders",
])
def test_required_physical_output_cannot_be_satisfied_outside_projection(sql: str) -> None:
    findings = validate_sql_semantics(sql, SemanticContract(outputs=["departments.name"]), CONTEXT)
    assert any(item.code == "REQUIRED_PHYSICAL_OUTPUT_MISSING" and item.status == "FAIL"
               for item in findings)


@pytest.mark.parametrize("sql", [
    "SELECT d.name AS department FROM departments d",
    "WITH x AS (SELECT name AS department FROM departments) SELECT department FROM x",
])
def test_output_aliases_and_simple_cte_lineage_preserve_source_identity(sql: str) -> None:
    findings = validate_sql_semantics(sql, SemanticContract(outputs=["departments.name"]), CONTEXT)
    assert any(item.code == "REQUIRED_PHYSICAL_OUTPUT_PRESENT" and item.status == "PASS"
               for item in findings)


@pytest.mark.parametrize("sql", [
    "SELECT MAX(name) FROM departments",
    "SELECT UPPER(name) FROM departments",
    'SELECT "name" FROM departments',
    "SELECT * FROM departments",
    "SELECT name FROM departments UNION ALL SELECT name FROM departments",
])
def test_unproven_transforms_and_scopes_are_not_falsely_certified(sql: str) -> None:
    findings = validate_sql_semantics(sql, SemanticContract(outputs=["departments.name"]), CONTEXT)
    output_findings = [item for item in findings
                       if item.code.startswith("REQUIRED_PHYSICAL_OUTPUT")]
    assert output_findings and all(item.status == "UNRESOLVED" for item in output_findings)


@pytest.mark.parametrize("repair", [False, True])
def test_omitted_output_never_executes_and_existing_bounded_repair_can_fix_it(
    registry: Any, monkeypatch: pytest.MonkeyPatch, repair: bool,
) -> None:
    executions = []
    execute = registry.execute

    def recorded(db_id: str, sql: str, **kwargs: Any) -> Any:
        executions.append(sql)
        return execute(db_id, sql, **kwargs)

    monkeypatch.setattr(registry, "execute", recorded)
    wrong, correct = "SELECT country FROM customers", "SELECT name FROM customers ORDER BY name"
    model = FakeModel([wrong, correct] if repair else [wrong])
    result = asyncio.run(TextToSQLAgent(registry, model).generate(GenerateRequest(
        db_id="shop", question="List customer names", execute=True,
        max_attempts=2 if repair else 1,
        semantic_contract=SemanticContract(outputs=["customers.name"]),
    )))
    assert result.accepted == repair
    assert wrong not in executions
    assert result.attempts[0].validation.code == "REQUIRED_PHYSICAL_OUTPUT_MISSING"
    assert len(result.attempts) == (2 if repair else 1)
    if repair:
        assert result.rows == [["Anna"], ["Luca"]]
        assert "REQUIRED_PHYSICAL_OUTPUT_MISSING" in model.feedback[1]
    else:
        assert executions == [] and result.rows == [] and result.sql is None


def test_aggregate_input_is_unresolved_not_forced_into_raw_projection() -> None:
    findings = validate_sql_semantics(
        "SELECT SUM(amount) FROM orders", SemanticContract(outputs=["orders.amount"]), CONTEXT)
    assert any(item.code == "REQUIRED_PHYSICAL_OUTPUT_UNPROVEN" for item in findings)
    assert not any(item.status == "FAIL" for item in findings)


def test_verified_names_with_spaces_are_unresolved_not_silently_ignored() -> None:
    context = {"tables": {"Sales Orders": {"columns": {"Total Value": {}}}}}
    findings = validate_sql_semantics(
        'SELECT 1 FROM "Sales Orders"',
        SemanticContract(outputs=["Sales Orders.Total Value"]), context)
    assert any(item.code == "REQUIRED_PHYSICAL_OUTPUT_UNPROVEN" and item.status == "UNRESOLVED"
               for item in findings)
