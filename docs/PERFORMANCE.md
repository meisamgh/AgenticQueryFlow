# Local performance measurements

For the V6.4 metadata-tool and prompt comparison, see
[UNIFIED_TOOLS.md](UNIFIED_TOOLS.md). Its scripted workload is separate from
the SQLGlot benchmark below.

These are **synthetic local measurements**, not production latency or SQL accuracy.
They use a fixed SQL model, one SQLite database, and BM25 fallback; network-model
latency, FastEmbed download time, and concurrent production traffic are excluded.
Run `.venv/bin/python benchmarks/benchmark_runtime.py` to repeat the cold/warm
API and transfer measurements.

| Path | Before | After | Measurement |
| --- | ---: | ---: | --- |
| History append, mean of writes 81–100 | 11.253 ms | 0.872 ms | 100 records in one SQLite-backed session; 500-row response payload |
| History append, 100-write total | 625.0 ms | 82.9 ms | Same synthetic workload |
| 100-entry in-process result cache peak | 5,513.9 KiB | 4,857.8 KiB | `tracemalloc`; each entry has 500 result rows |
| 500-row JSON response body | 15,135 bytes | 5,133 bytes | `gzip.compress(..., compresslevel=5)` microbenchmark |
| Serialization and compression CPU | 0.166 ms JSON | 0.082 ms additional GZip | Mean of 200 calls; same response |

The response-byte comparison is **uncompressed versus compressed**, not a
claim that JSON serialization itself became faster. The cache measurement is a
peak-allocation comparison; it includes temporary construction allocations.
History append is the clearest measured improvement: the old code parsed up to
500 complete records on every write, while the new path reads the latest record
and the relevant parent only.

Post-change end-to-end API example from the benchmark script:

| Mode | Requests | Median / p95 latency | Model calls | DB ledger calls | Median transfer bytes | Decoded bytes | Cumulative peak traced memory |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Cold, identity | 1 | 39.84 / 39.84 ms | 1 | 2 | 22,159 | 22,159 | 1,680.7 KiB |
| Warm, identity | 10 | 29.58 / 31.96 ms | 1 | 2 | 22,158 | 22,158 | 3,242.7 KiB |
| Warm, GZip | 10 | 31.25 / 31.66 ms | 1 | 2 | 6,739 | 22,158 | 4,607.4 KiB |

The warm observations are ten sequential requests; the cold row is one request.
The traced-memory peak is cumulative across modes, so it is not a per-request
memory comparison. There is no matched pre-change API-latency sample. Do not
infer a percentage end-to-end speedup from these rows. GZip reduced transfer
bytes in this example but increased median latency by about 1.67 ms versus warm
identity, which may include run-to-run noise.

FastEmbed now initializes once under a lock. Application startup attempts a
bounded preload; failure or timeout leaves deterministic BM25/value retrieval
available. The startup cost has **not** been benchmarked with a real downloaded
embedding model on this machine. Static schema/profile retrieval text is cached
separately from question-matched glossary enrichment. A regression test checks
that warm and fresh retrieval rankings are equal for different glossary queries.

## SQLGlot evaluation and stage ledger (2026-10-28)

The request ledger now separates schema inspection from retrieval and grounding,
and records SQL validation, optimization evaluation, and execution durations.
`total_database_calls` is the shared request budget's actual count; the old
formula added execution and recovery counts a second time. These are accounting
corrections, not latency improvements.

For a fixed SQLite query, `SELECT id, label, amount FROM orders WHERE 1 = 1
ORDER BY id`, SQLGlot proposes a simplification. The result-equivalence check
executes both versions before the optimizer's timed comparison. That check now
also serves as the warm-up, removing two repeated executions while retaining
result equivalence, one timed run of each version, and final SQL execution.

| Scripted SQLite path | Before | After | Notes |
| --- | ---: | ---: | --- |
| Warm median, 10 requests | 63.03 ms | 52.09 ms | Sequential local API requests |
| Warm p95, 10 requests | 64.69 ms | 52.53 ms | Nearest-rank p95 |
| Metered database calls/request | 8 | 6 | Includes schema inspection |
| Model calls/request | 1 | 1 | Fixed mock SQL model |
| Provider-reported tokens | 0 | 0 | Mock model reports no usage |
| Returned rows | 500 | 500 | Same fixed query and database |

On the after run, the last request ledger reported 0 ms schema inspection,
1 ms retrieval, 0 ms grounding, 1 ms mocked SQL generation, 2 ms validation,
22 ms optimizer evaluation, and 4 ms final execution. Remaining local latency
includes request handling, response construction, and serialization. Stage
times are rounded milliseconds and are not additive proof of the critical path.
`benchmark_assistant.py` separately measured one mock Top-layer call for a
greeting and two for a new SQL request; the latter includes a post-tool answer.
No live-provider, recovery, plotting, PostgreSQL, concurrent-load, or
semantic-accuracy improvement is claimed from this run.

Repeat with:

```sh
TEXT2SQL_BENCHMARK_SQL='SELECT id, label, amount FROM orders WHERE 1 = 1 ORDER BY id' \
  PYTHONPATH=src python benchmarks/benchmark_runtime.py
PYTHONPATH=src python benchmarks/benchmark_assistant.py
```

The next benchmark should use a fixed multi-turn corpus with live configured
providers, record model input/output and cache tokens, and score SQL result
equality plus semantic correctness. Do not remove the Top-layer final answer
or optional context planning on the strength of these mocked timings alone.
