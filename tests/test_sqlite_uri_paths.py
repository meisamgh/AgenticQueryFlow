"""Filesystem names must not become SQLite URI syntax."""
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from semantic_text2sql.database import DatabaseRegistry


def create_database(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE records (value TEXT)")
        connection.execute("INSERT INTO records VALUES (?)", (value,))


@pytest.mark.parametrize("folder", ["archive#2026", "archive?mode=rw", "archive%2Fdata",
                                     "archive with spaces", "données"])
def test_reserved_folder_names_open_exact_database_read_only(tmp_path: Path, folder: str) -> None:
    root = tmp_path / folder
    create_database(root / "shop" / "shop.sqlite", "intended")
    registry = DatabaseRegistry(root)
    assert [table.name for table in registry.inspect("shop").tables] == ["records"]
    assert registry.execute("shop", "SELECT value FROM records", max_rows=5) == (
        ["value"], [["intended"]], False,
    )
    with closing(registry.connect("shop")) as connection:
        connection.execute("PRAGMA query_only=OFF")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO records VALUES ('forbidden')")


def test_fragment_in_root_cannot_select_an_unrelated_database(tmp_path: Path) -> None:
    create_database(tmp_path / "warehouse", "unrelated")
    root = tmp_path / "warehouse#archive"
    create_database(root / "shop" / "shop.sqlite", "intended")
    result = DatabaseRegistry(root).execute("shop", "SELECT value FROM records", max_rows=5)
    assert result[1] == [["intended"]]
