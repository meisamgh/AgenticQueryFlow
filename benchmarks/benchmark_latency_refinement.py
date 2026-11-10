"""Paired BM25 CPU benchmark; no providers, credentials, SQL, or business data.

Run: .venv/bin/python benchmarks/benchmark_latency_refinement.py
The reference is the pre-refinement algorithm, not simulated model latency.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from statistics import median
from time import perf_counter

from semantic_text2sql.hybrid_retrieval import _bm25, _document_terms, _tokens


def reference(query: str, documents: dict[str, str]) -> dict[str, float]:
    query_tokens = _tokens(query)
    tokenized = {key: _tokens(value) for key, value in documents.items()}
    count = len(tokenized)
    average = sum(map(len, tokenized.values())) / count if count else 1.0
    frequency = Counter(token for tokens in tokenized.values() for token in set(tokens))
    scores = {}
    for key, tokens in tokenized.items():
        counts = Counter(tokens)
        score = 0.0
        for token in query_tokens:
            if not counts[token]:
                continue
            inverse = math.log(1 + (count-frequency[token]+.5)/(frequency[token]+.5))
            denominator = counts[token]+1.5*(.25+.75*len(tokens)/max(average,1))
            score += inverse*counts[token]*2.5/denominator
        scores[key] = score
    return scores


def main() -> None:
    documents = {str(i): (f"table_{i//20} column_{i} customer country monthly consumption "
                          "amount currency segment ") * 3 for i in range(1000)}
    queries = ["monthly consumption country", "customer amount", "segment currency"]
    _document_terms.cache_clear()
    started = perf_counter()
    baseline = reference(queries[0], documents)
    cold_before_ms = (perf_counter()-started)*1000
    started = perf_counter()
    assert _bm25(queries[0], documents) == baseline
    cold_after_ms = (perf_counter()-started)*1000
    durations: dict[str, list[float]] = {"before": [], "after": []}
    for iteration in range(100):
        query = queries[iteration % len(queries)]
        results = {}
        # Alternate order to reduce first-run/CPU load bias.
        for mode in (("before", "after") if iteration % 2 else ("after", "before")):
            started = perf_counter()
            results[mode] = (reference if mode == "before" else _bm25)(query, documents)
            durations[mode].append((perf_counter()-started)*1000)
        assert results["before"] == results["after"]
    print(json.dumps({
        "scope": "synthetic BM25 CPU only; 1000 documents, 100 alternating paired requests",
        "cold_before_ms": round(cold_before_ms, 3),
        "cold_after_ms": round(cold_after_ms, 3),
        "scores_identical": True, "model_calls": 0, "database_calls": 0,
        "tokens": 0,
        "measurements": {mode: {"p50_ms": round(median(values), 3),
                                 "p95_ms": round(sorted(values)[94], 3)}
                         for mode, values in durations.items()},
        "cache": str(_document_terms.cache_info()),
    }, indent=2))


if __name__ == "__main__":
    main()
