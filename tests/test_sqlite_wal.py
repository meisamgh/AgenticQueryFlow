"""Committed WAL data and schema must be visible to read-only readers."""
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from semantic_text2sql.database import DatabaseRegistry


def database_path(tmp_path: Path) -> Path:
    path = tmp_path / "shop" / "shop.sqlite"
    path.parent.mkdir()
    return path


def seed(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE records (value INTEGER)")
    connection.execute("INSERT INTO records VALUES (1)")
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def test_read_only_execution_includes_committed_wal_not_uncommitted_data(tmp_path: Path) -> None:
    path = database_path(tmp_path)
    with closing(sqlite3.connect(path)) as writer:
        seed(writer)
        registry = DatabaseRegistry(tmp_path)
        assert registry.execute("shop", "SELECT value FROM records", max_rows=10)[1] == [[1]]
        writer.execute("INSERT INTO records VALUES (2)")
        writer.commit()
        writer.execute("INSERT INTO records VALUES (3)")
        rows = registry.execute("shop", "SELECT value FROM records ORDER BY value", max_rows=10)[1]
        assert rows == [[1], [2]]
        with closing(registry.connect("shop")) as reader:
            reader.execute("PRAGMA query_only=OFF")
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                reader.execute("DELETE FROM records")


def test_schema_cache_refreshes_for_wal_only_schema_commit(tmp_path: Path) -> None:
    path = database_path(tmp_path)
    with closing(sqlite3.connect(path)) as writer:
        seed(writer)
        registry = DatabaseRegistry(tmp_path)
        original = registry.inspect("shop")
        assert registry.inspect("shop") is original
        modified = path.stat().st_mtime_ns
        writer.execute("ALTER TABLE records ADD COLUMN category TEXT")
        writer.commit()
        assert path.stat().st_mtime_ns == modified
        refreshed = registry.inspect("shop")
        assert [column.name for column in refreshed.tables[0].columns] == ["value", "category"]
        assert registry.inspect("shop") is refreshed


def test_schema_inspection_uses_one_snapshot_during_concurrent_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = database_path(tmp_path)
    with closing(sqlite3.connect(path)) as writer:
        seed(writer)
        registry = DatabaseRegistry(tmp_path)
        connect = registry.connect
        changed = False

        def injected_connect(db_id: str) -> Any:
            reader = connect(db_id)

            def execute(sql: str) -> Any:
                nonlocal changed
                result = reader.execute(sql)
                if "FROM sqlite_master" in sql and not changed:
                    changed = True
                    writer.execute("ALTER TABLE records ADD COLUMN category TEXT")
                    writer.commit()
                return result

            return SimpleNamespace(execute=execute, close=reader.close)

        monkeypatch.setattr(registry, "connect", injected_connect)
        snapshot = registry.inspect("shop")
        assert changed
        assert "category" not in snapshot.tables[0].create_sql
        assert [column.name for column in snapshot.tables[0].columns] == ["value"]
        latest = registry.inspect("shop")
        assert [column.name for column in latest.tables[0].columns] == ["value", "category"]
