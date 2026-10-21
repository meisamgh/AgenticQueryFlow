"""SQLGlot-aware changelog between two SQL versions for the visual SQL diff.

Only structural edits are reported. Formatting-only changes produced by the
same parse tree must not create a diff. When either statement cannot be parsed
the caller receives a conservative word-level fallback, so the UI never fails
purely because of an invalid candidate.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

import sqlglot
from pydantic import BaseModel, ConfigDict, Field
from sqlglot import exp
from sqlglot.diff import Insert, Remove, Update
from sqlglot.errors import ParseError, TokenError


class SqlDiffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    previous_sql: str = Field(min_length=1, max_length=20_000)
    updated_sql: str = Field(min_length=1, max_length=20_000)
    dialect: str = Field(default="sqlite", max_length=20)


class SqlDiffChange(BaseModel):
    kind: str = Field(max_length=10)
    token: str = Field(max_length=4_000)


class SqlDiffResponse(BaseModel):
    parse_error: bool
    changes: list[SqlDiffChange] = Field(default_factory=list, max_length=200)
    invariants: dict[str, str] = Field(default_factory=dict)
    filter_changes: list[dict[str, str | None]] = Field(default_factory=list)


def _features(query: exp.Expression, dialect: str) -> dict[str, list[str]]:
    return {
        "output expressions": (
            [item.sql(dialect=dialect) for item in query.selects]
            if isinstance(query, exp.Query)
            else []
        ),
        "grouping": [item.sql(dialect=dialect) for item in query.find_all(exp.Group)],
        "aggregations": sorted(item.sql(dialect=dialect) for item in query.find_all(exp.AggFunc)),
        "ranking / limit": [
            item.sql(dialect=dialect)
            for item in query.walk()
            if isinstance(item, (exp.Order, exp.Limit))
        ],
    }


def _filters(query: exp.Expression, dialect: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for where in query.find_all(exp.Where):
        for predicate in where.find_all(exp.Predicate):
            column = predicate.this
            if isinstance(column, exp.Column):
                key = f"{column.sql(dialect=dialect)} ({predicate.key})"
                result[key] = predicate.sql(dialect=dialect)
    return result


def _parse(sql: str, dialect: str) -> exp.Expression:
    value = sql.strip().rstrip(";").strip()
    if not value:
        raise ParseError("Empty SQL")
    return sqlglot.parse_one(value, read=dialect)


def _edit_entries(edit: Any, dialect: str) -> list[tuple[str, str]]:
    if isinstance(edit, Remove):
        return [("remove", edit.expression.sql(dialect=dialect))]
    if isinstance(edit, Insert):
        return [("insert", edit.expression.sql(dialect=dialect))]
    if isinstance(edit, Update):
        return [
            ("remove", edit.source.sql(dialect=dialect)),
            ("insert", edit.target.sql(dialect=dialect)),
        ]
    return []


def _words(sql: str) -> list[str]:
    return [part for part in re.split(r"\s+", " ".join(sql.split())) if part]


def _word_diff(previous: str, updated: str) -> list[tuple[str, str]]:
    left, right = _words(previous), _words(updated)
    changes: list[tuple[str, str]] = []
    for tag, start, end, bstart, bend in SequenceMatcher(None, left, right).get_opcodes():
        removed = " ".join(left[start:end])
        inserted = " ".join(right[bstart:bend])
        if tag in {"delete", "replace"} and removed:
            changes.append(("remove", removed[:4_000]))
        if tag in {"insert", "replace"} and inserted:
            changes.append(("insert", inserted[:4_000]))
    return changes


def _response(parse_error: bool, edits: list[tuple[str, str]]) -> SqlDiffResponse:
    return SqlDiffResponse(
        parse_error=parse_error,
        changes=[SqlDiffChange(kind=kind, token=token) for kind, token in edits[:200]],
    )


def sql_changelog(
    previous_sql: str, updated_sql: str, *, dialect: str = "sqlite"
) -> SqlDiffResponse:
    """Return typed edits between two SQL strings.

    ``parse_error`` is set when SQLGlot parsing failed; the fallback then
    compares whitespace-normalized words so a broken candidate still shows a
    textual difference instead of an unexplained empty diff.
    """
    try:
        previous = _parse(previous_sql, dialect)
    except (ParseError, TokenError):
        return _response(True, _word_diff(previous_sql or "", updated_sql or ""))
    try:
        updated = _parse(updated_sql, dialect)
    except (ParseError, TokenError):
        return _response(True, [("insert", " ".join((updated_sql or "").split())[:4_000])])
    changes = []
    for edit in sqlglot.diff(previous, updated):
        changes.extend(_edit_entries(edit, dialect))
    if not changes and previous.sql(dialect=dialect) != updated.sql(dialect=dialect):
        return _response(
            True,
            _word_diff(
                previous.sql(dialect=dialect),
                updated.sql(dialect=dialect),
            ),
        )
    left_features, right_features = _features(previous, dialect), _features(updated, dialect)
    left_filters, right_filters = _filters(previous, dialect), _filters(updated, dialect)
    filter_changes = [
        {
            "operation": (
                "REPLACE_FILTER"
                if target in left_filters and target in right_filters
                else "REMOVE_FILTER"
                if target in left_filters
                else "ADD_FILTER"
            ),
            "target": target,
            "previous_value": left_filters.get(target),
            "new_value": right_filters.get(target),
        }
        for target in sorted(set(left_filters) | set(right_filters))
        if left_filters.get(target) != right_filters.get(target)
    ]
    return _response(False, changes).model_copy(
        update={
            "invariants": {
                key: "unchanged" if left_features[key] == right_features[key] else "changed"
                for key in left_features
            },
            "filter_changes": filter_changes,
        }
    )
