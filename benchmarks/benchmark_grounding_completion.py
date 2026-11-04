"""Offline metadata-only measurements; no model calls or row queries."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any

from semantic_text2sql.database import DatabaseRegistry
from semantic_text2sql.join_resolver import resolve_joins
from semantic_text2sql.semantic_graph import SemanticGraphStore, compile_graph


def measure(operation: Any) -> dict[str, Any]:
    durations = []
    outcome = None
    for _ in range(101):
        started = perf_counter()
        outcome = operation()
        durations.append((perf_counter() - started) * 1000)
    result = {"p50_ms": median(durations), "p95_ms": sorted(durations)[95]}
    if hasattr(outcome, "status"):
        result.update(status=outcome.status,
                      examined_states=getattr(outcome, "examined_states", None),
                      warning_count=len(getattr(outcome, "warnings", [])))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-root", type=Path, required=True)
    args = parser.parse_args()
    registry = DatabaseRegistry(args.database_root)
    schemas = [registry.inspect(name) for name in registry.list_ids()]
    schema = max(schemas, key=lambda item: sum(len(table.columns) for table in item.tables))
    snapshot = compile_graph(schema)
    store = SemanticGraphStore(Path("/private/tmp/queryflow-no-persistent-graph"),
                               persistence_enabled=False)
    store.remember(snapshot)
    print(json.dumps({
        "schema": {"db_id": schema.db_id, "tables": len(schema.tables),
                   "columns": sum(len(table.columns) for table in schema.tables)},
        "compile": measure(lambda: compile_graph(schema).network()),
        "memory_snapshot": measure(lambda: store.load(schema.db_id, identity=snapshot.identity)),
        "memory_network": measure(lambda: store.network(schema.db_id, identity=snapshot.identity)),
        "traversal": measure(lambda: resolve_joins(
            [table.name for table in schema.tables][:5], schema, snapshot=snapshot)),
        "cached_network_traversal": measure(lambda: resolve_joins(
            [table.name for table in schema.tables][:5], schema, snapshot=snapshot,
            compiled_network=store.network(schema.db_id, identity=snapshot.identity))),
        "llm_calls": 0, "business_row_queries": 0,
    }, indent=2))


if __name__ == "__main__":
    main()
