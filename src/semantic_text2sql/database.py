"""Contained SQLite discovery, schema inspection, and read-only execution."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable
from contextlib import closing, suppress
from pathlib import Path
from time import monotonic
from typing import Any

from semantic_text2sql.models import (
    ColumnInfo,
    ForeignKeyInfo,
    SchemaInfo,
    TableInfo,
    ValidationResult,
)


class DatabaseError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


type FileStamp = tuple[int, int, int, int, int, int]
type SQLiteStamp = tuple[FileStamp, FileStamp | None]


def _file_stamp(path: Path) -> FileStamp:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns,
            stat.st_ctime_ns, stat.st_mode)


def _database_stamp(path: Path) -> SQLiteStamp:
    main = _file_stamp(path)
    try:
        wal = _file_stamp(path.with_name(path.name + "-wal"))
    except FileNotFoundError:
        wal = None
    return main, wal


class DatabaseRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self._schema_cache: dict[str, tuple[SQLiteStamp, SchemaInfo]] = {}

    def resolve(self, db_id: str) -> Path:
        if not db_id or any(not (char.isalnum() or char in "_-") for char in db_id):
            raise DatabaseError("DATABASE_ID_INVALID", "Unsupported database ID.")
        path = (self.root / db_id / f"{db_id}.sqlite").resolve()
        if self.root not in path.parents:
            raise DatabaseError("DATABASE_PATH_REJECTED", "Database escaped configured root.")
        if not path.is_file():
            raise DatabaseError("DATABASE_NOT_FOUND", f"Database {db_id} was not found.")
        return path

    def list_ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            directory.name
            for directory in self.root.iterdir()
            if directory.is_dir() and (directory / f"{directory.name}.sqlite").is_file()
        )

    def connect(self, db_id: str) -> sqlite3.Connection:
        path = self.resolve(db_id)
        # Encode filesystem characters before appending URI options. Otherwise
        # fragments/query delimiters can redirect SQLite to a different file.
        # A read-only database may still change through another writer. Do not
        # declare it immutable: SQLite would then ignore committed WAL pages.
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
            connection.execute("PRAGMA query_only = ON")
        except sqlite3.Error as exc:
            if connection is not None:
                with suppress(sqlite3.Error):
                    connection.close()
            raise DatabaseError(
                "DATABASE_CONNECTION_FAILED",
                "The read-only database connection could not be opened.",
            ) from exc
        return connection

    def inspect(self, db_id: str) -> SchemaInfo:
        try:
            return self._inspect(db_id)
        except DatabaseError:
            self._schema_cache.pop(db_id, None)
            raise
        except (sqlite3.Error, OSError) as exc:
            self._schema_cache.pop(db_id, None)
            raise DatabaseError(
                "DATABASE_INSPECTION_FAILED", "The database schema could not be inspected safely."
            ) from exc

    def _inspect(self, db_id: str) -> SchemaInfo:
        path = self.resolve(db_id)
        stamp = _database_stamp(path)
        cached = self._schema_cache.get(db_id)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        tables: list[TableInfo] = []
        relationships: list[ForeignKeyInfo] = []
        with closing(self.connect(db_id)) as connection:
            # Inspect all tables and keys from one coherent read snapshot.
            connection.execute("BEGIN")
            records = connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            if len(records) > 100:
                raise DatabaseError("SCHEMA_TOO_LARGE", "Database has more than 100 tables.")
            for table_name, create_sql in records:
                quoted = str(table_name).replace('"', '""')
                raw_columns = connection.execute(f'PRAGMA table_info("{quoted}")').fetchall()
                columns = [
                    ColumnInfo(
                        name=str(row[1]),
                        data_type=str(row[2] or "UNKNOWN"),
                        primary_key=bool(row[5]),
                        nullable=not bool(row[3]),
                    )
                    for row in raw_columns
                ]
                tables.append(
                    TableInfo(
                        name=str(table_name),
                        create_sql=str(create_sql or ""),
                        columns=columns,
                    )
                )
                for row in connection.execute(f'PRAGMA foreign_key_list("{quoted}")'):
                    target_table = str(row[2])
                    target_column = row[4]
                    if target_column is None:
                        target_quoted = target_table.replace('"', '""')
                        target_columns = connection.execute(
                            f'PRAGMA table_info("{target_quoted}")'
                        ).fetchall()
                        primary_columns = [
                            str(column[1]) for column in target_columns if bool(column[5])
                        ]
                        if len(primary_columns) != 1:
                            continue
                        target_column = primary_columns[0]
                    relationships.append(
                        ForeignKeyInfo(
                            from_table=str(table_name),
                            from_column=str(row[3]),
                            to_table=target_table,
                            to_column=str(target_column),
                            constraint_id=f"{table_name}:{row[0]}",
                            ordinal=int(row[1]),
                        )
                    )
        relationships = list(
            {
                (
                    item.from_table,
                    item.from_column,
                    item.to_table,
                    item.to_column,
                ): item
                for item in relationships
            }.values()
        )
        schema = SchemaInfo(db_id=db_id, tables=tables, relationships=relationships)
        self._schema_cache[db_id] = (stamp, schema)
        return schema

    def explain(self, db_id: str, sql: str, validation: ValidationResult) -> ValidationResult:
        with closing(self.connect(db_id)) as connection:
            try:
                rows = connection.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
            except sqlite3.Error as exc:
                return validation.model_copy(
                    update={
                        "valid": False,
                        "code": "SQL_EXPLAIN_FAILED",
                        "message": f"SQLite rejected the query: {exc}",
                    }
                )
        return validation.model_copy(
            update={"explain_plan": [" | ".join(map(str, row)) for row in rows]}
        )

    def execute(
        self,
        db_id: str,
        sql: str,
        *,
        max_rows: int,
        timeout_seconds: float = 5.0,
        parameters: tuple[Any, ...] = (),
    ) -> tuple[list[str], list[list[Any]], bool]:
        deadline = monotonic() + timeout_seconds
        with closing(self.connect(db_id)) as connection:
            connection.set_progress_handler(lambda: 1 if monotonic() > deadline else 0, 1_000)
            try:
                cursor = connection.execute(sql, parameters)
                rows = cursor.fetchmany(max_rows + 1)
            except sqlite3.Error as exc:
                raise DatabaseError("SQL_EXECUTION_FAILED", str(exc)) from exc
        columns = [item[0] for item in cursor.description or ()]
        return columns, [_json_row(row) for row in rows[:max_rows]], len(rows) > max_rows


def _json_row(row: Iterable[Any]) -> list[Any]:
    values: list[Any] = []
    for value in row:
        if isinstance(value, float) and not math.isfinite(value):
            raise DatabaseError(
                "SQL_RESULT_NOT_JSON_SAFE",
                "The query returned a non-finite number that cannot be represented safely. "
                "Review overflow or non-finite source values; no value was replaced.",
            )
        values.append(
            value if value is None or isinstance(value, str | int | float | bool) else str(value)
        )
    return values
