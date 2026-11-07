"""Non-finite SQL numbers must fail structurally, not break JSON responses."""
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from semantic_text2sql.agent import TextToSQLAgent
from semantic_text2sql.api import create_app
from semantic_text2sql.database import DatabaseError, DatabaseRegistry, _json_row
from semantic_text2sql.postgres import _json_row as postgres_row
from semantic_text2sql.remote_databases import _row as remote_row


def test_sqlite_nonfinite_result_is_a_structured_database_failure(tmp_path: Path) -> None:
    directory = tmp_path / "sample"
    directory.mkdir()
    with sqlite3.connect(directory / "sample.sqlite") as connection:
        connection.execute("CREATE TABLE sample (value REAL)")
    registry = DatabaseRegistry(tmp_path)
    with pytest.raises(DatabaseError) as error:
        registry.execute("sample", "SELECT 1e999 AS total", max_rows=10)
    assert error.value.code == "SQL_RESULT_NOT_JSON_SAFE"
    assert "1e999" not in str(error.value)


@pytest.mark.parametrize("convert", [_json_row, postgres_row, remote_row])
@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_all_adapter_serializers_reject_nonfinite_numbers(convert, value) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(DatabaseError) as error:
        convert((value,))
    assert error.value.code == "SQL_RESULT_NOT_JSON_SAFE"


@pytest.mark.parametrize("convert", [_json_row, postgres_row, remote_row])
def test_finite_numbers_and_nulls_remain_unchanged(convert) -> None:  # type: ignore[no-untyped-def]
    values = (None, 0, -4, 1.25, 1e300, "NaN", True)
    row = convert(values)
    assert row == list(values)
    assert JSONResponse(row).status_code == 200


def test_admin_check_returns_safe_structured_result_error(registry, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TEXT2SQL_ENABLE_ADMIN_CHECK_EXECUTION", "true")
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    model = Mock()
    client = TestClient(create_app(TextToSQLAgent(registry, model)))
    response = client.post("/api/check", json={
        "db_id": "shop", "sql": "SELECT 1e999 AS total", "execute": True,
    })
    assert response.status_code == 503
    assert response.json()["code"] == "SQL_RESULT_NOT_JSON_SAFE"
    assert "non-finite" in response.json()["detail"]
    assert "1e999" not in response.text
    model.generate.assert_not_called()
