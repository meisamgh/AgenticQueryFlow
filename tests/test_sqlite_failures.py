"""SQLite infrastructure failures must cross the structured error boundary."""
import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from semantic_text2sql.agent import TextToSQLAgent
from semantic_text2sql.api import create_app
from semantic_text2sql.database import DatabaseError, DatabaseRegistry
from semantic_text2sql.models import GenerateRequest


def corrupt_database(tmp_path: Path) -> DatabaseRegistry:
    path = tmp_path / "broken" / "broken.sqlite"
    path.parent.mkdir()
    path.write_bytes(b"synthetic invalid SQLite file")
    return DatabaseRegistry(tmp_path)


def test_corrupt_schema_is_a_structured_failure(tmp_path: Path) -> None:
    registry = corrupt_database(tmp_path)
    with pytest.raises(DatabaseError) as error:
        registry.inspect("broken")
    assert error.value.code == "DATABASE_INSPECTION_FAILED"
    assert str(tmp_path) not in str(error.value)
    assert "broken" not in registry._schema_cache


def test_corrupt_schema_api_returns_safe_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = corrupt_database(tmp_path)
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(tmp_path))
    client = TestClient(create_app(TextToSQLAgent(registry, Mock())))
    response = client.get("/api/databases/broken/schema")
    assert response.status_code == 503
    assert response.json()["code"] == "DATABASE_INSPECTION_FAILED"
    assert str(tmp_path) not in response.text


def test_corrupt_schema_stops_generation_without_model_call(tmp_path: Path) -> None:
    registry = corrupt_database(tmp_path)
    model = Mock()
    response = asyncio.run(TextToSQLAgent(registry, model).generate(
        GenerateRequest(db_id="broken", question="Count records", execute=True),
    ))
    assert not response.accepted
    assert response.termination_reason == "database_error"
    assert response.sql is None and response.rows == []
    model.generate.assert_not_called()


def test_connection_failure_is_structured(registry, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("semantic_text2sql.database.sqlite3.connect", Mock(
        side_effect=sqlite3.OperationalError("sensitive local path /private/example"),
    ))
    with pytest.raises(DatabaseError) as error:
        registry.connect("shop")
    assert error.value.code == "DATABASE_CONNECTION_FAILED"
    assert "/private/example" not in str(error.value)


def test_failed_read_only_initialization_closes_connection(registry, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    connection = Mock()
    connection.execute.side_effect = sqlite3.DatabaseError("synthetic initialization error")
    monkeypatch.setattr("semantic_text2sql.database.sqlite3.connect", Mock(return_value=connection))
    with pytest.raises(DatabaseError) as error:
        registry.connect("shop")
    assert error.value.code == "DATABASE_CONNECTION_FAILED"
    connection.close.assert_called_once()


def test_inspection_stat_failure_is_structured_and_evicts_cached_schema(
    registry, monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    registry.inspect("shop")
    monkeypatch.setattr("semantic_text2sql.database._database_stamp", Mock(
        side_effect=PermissionError("sensitive local path /private/example"),
    ))
    with pytest.raises(DatabaseError) as error:
        registry.inspect("shop")
    assert error.value.code == "DATABASE_INSPECTION_FAILED"
    assert "/private/example" not in str(error.value)
    assert "shop" not in registry._schema_cache
