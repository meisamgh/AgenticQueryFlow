import json

import pytest
from pydantic import ValidationError

from semantic_text2sql.database import DatabaseError
from semantic_text2sql.external_connections import (
    ExternalConnection,
    external_connections_from_environment,
)


@pytest.mark.parametrize("engine,scheme,dialect", [
    ("mysql", "mysql+pymysql", "mysql"), ("sqlserver", "mssql+pyodbc", "tsql"),
    ("snowflake", "snowflake", "snowflake"), ("bigquery", "bigquery", "bigquery"),
])
def test_external_configuration_is_lazy_and_keeps_credentials_out_of_public_data(
    monkeypatch: pytest.MonkeyPatch, engine: str, scheme: str, dialect: str,
) -> None:
    raw = {"db_id":"business", "engine":engine, "url_env":"BUSINESS_DATABASE_URL",
           "schema_name":"project.analytics" if engine == "bigquery" else "analytics",
           "allowed_tables":["orders"],
           "maximum_bytes_billed":1000000}
    monkeypatch.setenv("TEXT2SQL_EXTERNAL_CONNECTIONS", json.dumps([raw]))
    monkeypatch.setenv("BUSINESS_DATABASE_URL", f"{scheme}://private-secret")
    connection = external_connections_from_environment()["business"]
    assert connection.dialect == dialect
    assert "private-secret" not in connection.model_dump_json()
    assert connection.connection_url() == f"{scheme}://private-secret"
    monkeypatch.delenv("BUSINESS_DATABASE_URL")
    with pytest.raises(DatabaseError):
        connection.connection_url()


def test_external_configuration_rejects_ambiguous_or_unbounded_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = {"db_id":"business", "engine":"mysql", "url_env":"BUSINESS_DATABASE_URL",
           "schema_name":"analytics", "allowed_tables":["orders"]}
    monkeypatch.setenv("TEXT2SQL_EXTERNAL_CONNECTIONS", json.dumps([raw, raw]))
    with pytest.raises(ValueError, match="Duplicate"):
        external_connections_from_environment()
    with pytest.raises(ValidationError):
        ExternalConnection.model_validate({**raw, "engine":"bigquery"})
    with pytest.raises(ValidationError):
        ExternalConnection.model_validate({**raw, "allowed_tables":["orders; DROP TABLE orders"]})
