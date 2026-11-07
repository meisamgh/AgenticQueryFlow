"""Paired offline KPI-grounding benchmark; synthetic fixtures, no model inference.

Run from repository root: uv run python benchmarks/benchmark_kpi_first.py
The development/test dependencies are required. JSON goes to stdout; no rows or
credentials are persisted. This is NOT an LLM SQL-accuracy benchmark.
"""
from __future__ import annotations

import json
import statistics
import sys
import tempfile
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_kpi_first import Encoder, fixture

from semantic_text2sql.graph_grounding import resolve_metrics
from semantic_text2sql.hybrid_retrieval import HybridSchemaRetriever
from semantic_text2sql.join_resolver import resolve_joins
from semantic_text2sql.kpi_resolution import build_kpi_grounding, resolve_kpi_candidates
from semantic_text2sql.models import SemanticContract
from semantic_text2sql.semantic_graph import compile_graph


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="queryflow-kpi-benchmark-") as folder:
        registry, document = fixture(Path(folder))
        document.metrics[0].aliases.append("return adjusted sales")
        schema = registry.inspect("shop")
        snapshot = compile_graph(schema, document)
        network = snapshot.network()
        cases = [("Net revenue", "net_revenue"),
                 ("Sales after refunds by department", "net_revenue"),
                 ("Return-adjusted sales by department", "net_revenue"),
                 ("Gross sales", "gross_sales"), ("Refunds", "refunds")]
        results = {}
        for mode in ("baseline", "kpi_first"):
            retriever = HybridSchemaRetriever(Encoder(), max_tables=1, max_columns_per_table=1)
            times, measurements = [], []
            for iteration in range(51):
                for question, expected in cases:
                    start = perf_counter()
                    contract = SemanticContract()
                    resolution = resolve_kpi_candidates(question, schema, document) if (
                        mode == "kpi_first") else None
                    contract, facts = resolve_metrics(
                        question, schema, document, contract,
                        metric_ids=resolution.selected_ids if resolution else None,
                        strict_dependencies=mode == "kpi_first")
                    pin = build_kpi_grounding(resolution, contract, document, question) if (
                        resolution is not None) else None
                    _, selection, _ = retriever.retrieve(
                        question, None, schema, None, None,
                        mandatory_columns=pin.mandatory_columns if pin else (),
                        mandatory_tables=pin.mandatory_tables if pin else (),
                        defer_join_expansion=bool(pin),
                    )
                    joins = resolve_joins(selection.tables, schema, snapshot=snapshot,
                                          compiled_network=network)
                    elapsed = (perf_counter() - start) * 1000
                    reference, _ = resolve_metrics(expected, schema, document, SemanticContract())
                    reference_resolution = resolve_kpi_candidates(question, schema, document)
                    reference_grounding = build_kpi_grounding(
                        reference_resolution.model_copy(update={"selected_ids": [expected]}),
                        reference, document, question)
                    required = set(reference_grounding.mandatory_columns)
                    observed = {f"{table}.{col}" for table, cols in selection.columns.items()
                                for col in cols}
                    roots = set(resolution.selected_ids) if resolution else (
                        {m.id for m in contract.approved_metrics}
                        - {dep for metric in facts for dep in metric["dependencies"]})
                    if iteration:
                        times.append(elapsed)
                        measurements.append((roots == {expected},
                            len(required & observed) / len(required),
                            {m.id for m in reference.approved_metrics}
                            <= {m.id for m in contract.approved_metrics},
                            set(reference_grounding.mandatory_tables) <= set(joins.tables)
                            and joins.status == "CONNECTED"))
            ordered = sorted(times)
            results[mode] = {
                "observations": len(times), "cases": len(cases), "warmup_passes": 1,
                "p50_ms": round(statistics.median(times), 3),
                "p95_ms": round(ordered[int(.95 * (len(ordered) - 1))], 3),
                "synthetic_root_identity_accuracy": statistics.mean(m[0] for m in measurements),
                "required_column_recall": statistics.mean(m[1] for m in measurements),
                "complete_dependency_recall": statistics.mean(m[2] for m in measurements),
                "required_join_plan_coverage": statistics.mean(m[3] for m in measurements),
                "llm_calls": 0, "llm_tokens": 0, "timed_database_calls": 0,
                "sql_generation_execution_accuracy": None,
                "independently_adjudicated_business_accuracy": None,
            }
        print(json.dumps({"scope": "offline synthetic paired grounding; scripted dense encoder",
                          "results": results}, indent=2))


if __name__ == "__main__":
    main()
