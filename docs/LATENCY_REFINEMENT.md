# Latency refinement — 2026-11-09

## Changes

- `hybrid_retrieval.py`: bounded, immutable BM25 term-frequency reuse by exact
  document text. At most 1,024 entries of at most 4,096 characters; larger documents
  are processed uncached. No cached scores, query-dependent ranking or database
  result cache is introduced. Changed glossary enrichment produces a different
  text key. Scores remain identical to the previous algorithm.
- `web/semantic_explorer.js`: reuse the current opening request's KPI overview
  instead of requesting it twice; avoid duplicate inspection after root KPI
  selection. Reconcile Cytoscape elements by stable ID and layout only on topology
  changes. Changed endpoints, removed nodes and changed metadata are handled.
  Selections and node objects survive metadata-only refresh. No cross-session
  graph-response cache or new authorization assumptions.
- `benchmarks/benchmark_runtime.py`: reference-scoring baseline switch, per-stage
  ledger timing summaries, explicit unavailable token counts, correct nearest-rank
  p95. Stages are reported separately, never summed into a fake request duration.
- `benchmarks/benchmark_latency_refinement.py`: alternating paired CPU benchmark
  against the previous scoring implementation, checking exact score equality.

No provider settings, credentials, SQL safety, accepted state, or LLM routing changed.

## Existing optimizations inspected and retained

| Recommendation | Current implementation/evidence |
| --- | --- |
| Reuse complete accepted results | `plotting_service.design_chart`, stable result IDs and completeness gate; `test_integrated_chart_planning.py` rejects missing IDs and incomplete rows |
| Combine decision and chart planning | `AgentDecision.chart_proposal`; validated by the chart service; existing tests prove zero plotting-model calls, including invalid-proposal fallback |
| Keep context cheap | `TextToSQLService` already forces retrieval-only context; no Model 1 call was added |
| Compact context/history | `_reason()` supplies accepted contracts, references and bounded profiles, with historical SQL available on demand; no full conversation/row injection added |
| Stop recovery on sufficient evidence | Existing `_verified_absent_requested_key` early exit; recovery tests preserve exact live-probe evidence and shared budgets |
| Reuse metadata | Existing schema/profile document cache and registry schema cache retained; no redundant SQLite cache |
| Avoid Explorer work on interaction | Selection only inspects metadata; changed topology alone triggers layout; search already requires explicit Enter/Search, not one request per keystroke |

Top-layer LLM remains the only conversational decision-maker. No natural-language
fast-path router, agent, framework, or additional LLM call was added.

## Measurements

Local synthetic measurements, not production/provider latency. Identical inputs
and score/graph-count assertions are used; no business data or external calls.

| Workload | Before p50 / p95 | After p50 / p95 |
| --- | --- | --- |
| BM25, 1,000 documents, 100 alternating paired requests | 5.776 / 9.934 ms | 0.984 / 1.280 ms |
| Unchanged Cytoscape refresh, 100 nodes/99 links, 40 paired requests | 5.780 / 14.604 ms | 0.320 / 0.778 ms |
| SQLite TestClient API, 500 returned rows, 10 warm requests | 41.20 / 45.41 ms | 39.88 / 49.77 ms |

BM25 cold CPU: 6.301 ms reference, 6.057 ms cached implementation. Headless graph
measurements cover reconciliation/layout, not browser paint. Initial KPI overview
requests decrease from two to one (regression assertion), with no extra SQL/LLM.

Small-schema API measurements do **not** establish an end-to-end speedup: p95
regressed in this run and process/load noise dominates. Cold API requests were
95.22 vs 100.65 ms. API call counts unchanged: one scripted SQL-model call, two
DATABASE ledger entries (schema inspection + query execution), zero provider
retries. Input/output tokens were unavailable from that scripted provider and
are reported as null, not invented. Warm peak traced memory was 3,955.3 vs
3,956.4 KiB; no memory-reduction claim. Both paths returned HTTP 200 and 500 rows.

After-run warm stage p50/p95 (ms): context grounding 1/3, schema inspection 2/4,
graph lookup 1/1, scripted SQL generation 1/1, validation 4/5, execution 6/8.
Model 1/recovery/plotting/final-answer stages were not executed in this SQL-only
fixture. GZip transferred approximately 7.2 KB instead of 25.9 KB; already-existing
compression, not a new optimization.

The separate scripted assistant benchmark reports greeting 20.599 ms median
(one decision call, zero SQL calls) and new query 21.267 ms (two assistant decisions,
one governed SQL-handler invocation). Those are mock orchestration costs, exclude
provider/network/real database time, and are not a before/after improvement claim.
No reduction in actual LLM token consumption is established by this patch.

## Verification and limitations

Regression additions cover exact BM25 equality, repeated query tokens, empty
documents, query-specific enrichment, immutable terms, oversized cache bypass,
initial overview request count, node identity, changed endpoints, removal, and
layout avoidance. Existing full regression suite continues to exercise SQL,
multi-turn state, recovery, chart provenance, result reuse and frontend behavior.
Full verification: 953 Python passed, 2 PostgreSQL tests skipped; 235 frontend and
voice tests passed; Ruff, mypy over 75 source files, and `git diff --check` passed.

Run:

```sh
.venv/bin/python benchmarks/benchmark_latency_refinement.py
.venv/bin/python benchmarks/benchmark_runtime.py --reference-bm25
.venv/bin/python benchmarks/benchmark_runtime.py
.venv/bin/python benchmarks/benchmark_assistant.py
deno test -A tests/semantic_explorer.cjs
```

Live-provider latency/tokens, real browser paint responsiveness, PostgreSQL and
production multi-user behavior were not benchmarked here. No server was restarted.
Next high-value measurement is the real provider critical path using existing
per-call ledgers on representative multi-turn questions; CPU savings must not be
reported as seconds saved in inference. Avoid removing the post-tool decision
when further reasoning is necessary merely to reduce a call count.
