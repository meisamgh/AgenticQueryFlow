# Local performance measurements

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
