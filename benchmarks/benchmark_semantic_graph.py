"""Offline graph benchmark and stored debit-card failure signals (no provider or DB calls)."""

from __future__ import annotations

import argparse
import json
import statistics
from collections import deque
from pathlib import Path
from time import perf_counter

from sqlglot import exp, parse_one

from semantic_text2sql.glossary import GlossaryStore
from semantic_text2sql.join_resolver import resolve_joins
from semantic_text2sql.models import ColumnInfo, ForeignKeyInfo, SchemaInfo, TableInfo
from semantic_text2sql.ontology import LocalOntologyAdapter
from semantic_text2sql.semantic_catalog import SemanticCatalogStore
from semantic_text2sql.semantic_graph import SemanticGraphStore, compile_graph
from semantic_text2sql.validator import physical_table_references


def legacy_bridge(schema: SchemaInfo) -> list[str]:
    graph: dict[str, set[str]] = {table.name: set() for table in schema.tables}
    for edge in schema.relationships:
        graph[edge.from_table].add(edge.to_table)
        graph[edge.to_table].add(edge.from_table)
    queue = deque([("a", ["a"])])
    seen = {"a"}
    while queue:
        current, path = queue.popleft()
        if current == "c":
            return path
        for neighbor in sorted(graph[current]):
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append((neighbor, [*path, neighbor]))
    return []


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    schema = SchemaInfo(
        db_id="benchmark",
        tables=[
            TableInfo(
                name=name,
                create_sql="",
                columns=[ColumnInfo(name="id", data_type="INTEGER", primary_key=True)],
            )
            for name in ("a", "b", "c")
        ],
        relationships=[
            ForeignKeyInfo(from_table=a, from_column="id", to_table=b, to_column="id")
            for a, b in [("a", "b"), ("b", "c")]
        ],
    )
    graph = compile_graph(schema)
    store = SemanticGraphStore(args.root)
    store.save(graph)
    measurements = {}
    ontology = LocalOntologyAdapter(schema, None, SemanticCatalogStore(), store, GlossaryStore())
    operations = {
        "legacy_bfs": lambda: legacy_bridge(schema),
        "shared_resolver": lambda: resolve_joins(["a", "c"], schema),
        "graph_resolver": lambda: resolve_joins(["a", "c"], schema, snapshot=graph),
        "graph_plus_local_ontology": lambda: (
            resolve_joins(["a", "c"], schema, snapshot=graph),
            ontology.lookup("join", allowed_tables={"a", "b", "c"}),
        ),
        "warm_load": lambda: store.load(schema.db_id, identity=graph.identity),
        "cold_load": lambda: SemanticGraphStore(args.root).load(
            schema.db_id, identity=graph.identity
        ),
        "offline_compile": lambda: compile_graph(schema),
    }
    for name, operation in operations.items():
        timings = []
        for _ in range(101):
            start = perf_counter()
            operation()
            timings.append((perf_counter() - start) * 1000)
        measurements[name] = {
            "p50_ms": statistics.median(timings),
            "p95_ms": sorted(timings)[95],
            "iterations": len(timings),
            "model_calls": 0,
            "database_calls": 0,
        }
    historical = json.loads(Path("benchmarks/results/debit_card_model1_ab_20.json").read_text())
    failures = []
    for row in historical["results"]:
        if row["retrieval"]["correct"] or row["model1"]["correct"]:
            continue
        gold = parse_one(row["gold_sql"], read="sqlite")
        expected_tables = {table.name for table in physical_table_references(gold)}
        signals = {}
        for mode in ("retrieval", "model1"):
            actual = parse_one(row[mode]["sql"], read="sqlite")
            tables = {table.name for table in physical_table_references(actual)}
            signals[mode] = {
                "retrieval_or_generation_missing_relations": sorted(expected_tables - tables),
                "join_or_generation_extra_relations": sorted(tables - expected_tables),
                "metric_semantics_aggregate_shape_differs": sorted(
                    node.key for node in gold.find_all(exp.AggFunc)
                )
                != sorted(node.key for node in actual.find_all(exp.AggFunc)),
                "filter_or_time_literals_missing": sorted(
                    {node.sql() for node in gold.find_all(exp.Literal)}
                    - {node.sql() for node in actual.find_all(exp.Literal)}
                ),
                "accepted_but_wrong_in_stored_evaluation": row[mode]["accepted"],
            }
        failures.append(
            {
                "question_id": row["question_id"],
                "signals": signals,
                "root_cause": "UNADJUDICATED: AST differences are not causal proof",
            }
        )
    report = {
        "scope": "Offline synthetic microbenchmark, not end-to-end accuracy/latency",
        "measurements": measurements,
        "historical_baseline_not_rerun": historical["summary"],
        "stored_paired_failures": failures,
        "not_run": [
            "fresh debit-card SQL execution",
            "business-semantic adjudication",
            "live model tokens",
            "recovery success/false repairs",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(measurements, indent=2))


if __name__ == "__main__":
    main()
