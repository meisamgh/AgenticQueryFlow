"""Optional Phase 0 measurements using existing read-only registry/validation APIs."""

from __future__ import annotations

import math
import statistics
import tempfile
from collections import Counter
from pathlib import Path
from time import perf_counter
from typing import Any

from sqlglot import exp, parse_one

from semantic_text2sql.database import DatabaseError, DatabaseRegistry
from semantic_text2sql.join_resolver import resolve_joins
from semantic_text2sql.semantic_graph import SemanticGraphStore, compile_graph
from semantic_text2sql.validator import validate_sql


def equivalent(predicted: list[list[Any]], gold: list[list[Any]], *, ordered: bool) -> bool:
    def normalized(rows: list[list[Any]]) -> list[tuple[Any, ...]]:
        return [
            tuple(
                "NaN"
                if isinstance(value, float) and math.isnan(value)
                else round(value, 6)
                if isinstance(value, float)
                else value
                for value in row
            )
            for row in rows
        ]

    a, b = normalized(predicted), normalized(gold)
    return a == b if ordered else Counter(a) == Counter(b)


def recheck_saved_sql(root: Path, artifact: dict[str, Any]) -> dict[str, Any]:
    registry = DatabaseRegistry(root)
    try:
        schema = registry.inspect("debit_card_specializing")
    except DatabaseError as exc:
        return {"status": "NOT_RUN", "reason": exc.code}
    records = []
    calls = 0
    for row in artifact["results"]:
        outputs: dict[str, list[list[Any]]] = {}
        checks = {}
        for mode in ("gold", "retrieval", "model1"):
            sql = row["gold_sql"] if mode == "gold" else row[mode]["sql"]
            validation = validate_sql(sql, schema)
            if not validation.valid:
                checks[mode] = {"status": "REJECTED", "code": validation.code}
                continue
            start = perf_counter()
            try:
                calls += 1
                _, rows, truncated = registry.execute(
                    "debit_card_specializing", sql, max_rows=500, timeout_seconds=5.0
                )
                checks[mode] = {
                    "status": "TRUNCATED" if truncated else "EXECUTED",
                    "rows": len(rows),
                    "execution_ms": (perf_counter() - start) * 1000,
                }
                if not truncated:
                    outputs[mode] = rows
            except DatabaseError as exc:
                checks[mode] = {"status": "FAILED", "code": exc.code}
        ordered = any(parse_one(row["gold_sql"], read="sqlite").find_all(exp.Order))
        comparisons = {}
        proofs = {}
        for mode in ("retrieval", "model1"):
            if mode in outputs and "gold" in outputs:
                comparisons[mode] = equivalent(outputs[mode], outputs["gold"], ordered=ordered)
                proofs[mode] = "COMPLETE_RETURNED_ROWS"
            elif {checks[mode]["status"], checks["gold"]["status"]} == {"TRUNCATED", "EXECUTED"}:
                # A truncated 500-row preview has >=501 rows; the other side is complete <=500.
                comparisons[mode] = False
                proofs[mode] = "DIFFERENT_ROW_COUNT_BY_TRUNCATION_LOWER_BOUND"
            else:
                comparisons[mode] = None
                proofs[mode] = "UNRESOLVED"
        records.append(
            {
                "question_id": row["question_id"],
                "checks": checks,
                "reference_match": comparisons,
                "comparison_proof": proofs,
                "stored_match": {mode: row[mode]["correct"] for mode in ("retrieval", "model1")},
            }
        )
    return {
        "status": "MEASURED",
        "model_calls": 0,
        "sql_execution_calls": calls,
        "metadata_inspections": 1,
        "row_limit": 500,
        "query_timeout_seconds": 5,
        "raw_rows_persisted": False,
        "cases": records,
        "note": "Uses the original comparator; reference agreement is not business correctness",
    }


def measured_metadata_graph(root: Path) -> dict[str, Any]:
    registry = DatabaseRegistry(root)
    schemas = [registry.inspect(identity) for identity in registry.list_ids()]
    if not schemas:
        return {"status": "NOT_RUN", "reason": "No accessible SQLite schemas"}
    schema = max(
        schemas,
        key=lambda item: (sum(len(table.columns) for table in item.tables), len(item.tables)),
    )
    graph = compile_graph(schema)
    measurements = {}
    with tempfile.TemporaryDirectory(prefix="queryflow-phase0-graph-") as directory:
        store = SemanticGraphStore(Path(directory))
        path = store.save(graph)
        size = path.stat().st_size
        operations = {
            "current_compile_and_networkx_build": lambda: compile_graph(schema).network(),
            "networkx_build_from_compiled_metadata": graph.network,
            "cold_snapshot_load": lambda: SemanticGraphStore(Path(directory)).load(
                schema.db_id, identity=graph.identity
            ),
            "warm_snapshot_load": lambda: store.load(schema.db_id, identity=graph.identity),
            "traversal": lambda: resolve_joins(
                [table.name for table in schema.tables][:5], schema, snapshot=graph
            ),
        }
        store.load(schema.db_id, identity=graph.identity)
        for name, operation in operations.items():
            durations = []
            outcome = None
            for _ in range(101):
                start = perf_counter()
                outcome = operation()
                durations.append((perf_counter() - start) * 1000)
            measurements[name] = {
                "p50_ms": statistics.median(durations),
                "p95_ms_nearest_rank": sorted(durations)[95],
                "iterations": 101,
            }
            if name == "traversal":
                measurements[name]["status"] = outcome.status
    return {
        "status": "MEASURED",
        "selection": "most columns, then most tables",
        "db_id": schema.db_id,
        "tables": len(schema.tables),
        "columns": sum(len(table.columns) for table in schema.tables),
        "relationships": len(schema.relationships),
        "schema_fingerprint": graph.schema_fingerprint,
        "traversal_seeds": [table.name for table in schema.tables][:5],
        "snapshot_bytes": size,
        "schemas_inspected": len(schemas),
        "model_calls": 0,
        "business_sql_calls": 0,
        "metrics": measurements,
        "note": "Existing metadata compiler only, no catalog authoring or runtime changes",
    }
