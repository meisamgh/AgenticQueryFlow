# Offline graph-enhanced semantic grounding

**Amendment gate:** [Phase 0 findings](SEMANTIC_GRAPH_PHASE0.md) supersede the
implementation order below. Existing code is retained; resolver ambiguity and
separate default-OFF persistence changes are pending review, not already complete.

The existing Top-layer LangGraph assistant remains the only conversational agent.
Hybrid retrieval still selects schema candidates; optional graph grounding adds
bounded relationship evidence and approved KPI dependencies before the existing
SQL generator, SQLGlot/semantic validation, read-only execution and recovery.
Dashboard code and analytical result storage are unchanged.

## Existing architecture and confirmed gaps

| Existing boundary | Confirmed finding / implementation |
|---|---|
| `hybrid_retrieval.py`, `linker.py`, `context.py` | Three independent traversal implementations now use `join_resolver.py`. No extra model call. |
| `semantic_catalog.py`, `semantic_catalog_validation.py` | Governed JSON facts, human approvals and evidence probes already existed. They remain authoritative; the graph is derived metadata. |
| `glossary.py` | Curated definitions already existed. Graph concept mappings remain advisory, not live value-existence evidence. |
| `service.py` | Shared governed SQL execution already existed. Optional graph/catalog enrichment is now attached here before generation, including the optional Model 1 path. |
| `validator.py` | Existing PASS/FAIL/UNRESOLVED semantic checks are retained. A wrong direct aggregate function for a simple approved metric now fails; derived expression equivalence remains unresolved. |
| `recovery.py` | Existing bounded reason/tool loop is reused for optional `query_ontology`; no second agent. |
| `conversational_agent.py` | Continues to call the existing governed service. No alternative intent router. |
| `autonomous_analytics/metrics/registry.py` | A separate analytics-package registry already exists; it is not copied into SQL grounding or silently promoted into approved catalog facts. |

## Interfaces and construction

- `compile_graph(schema, catalog, profile, glossary) -> GraphSnapshot`: deterministic,
  no LLM, row sampling, DB execution or external network. Typed TABLE/COLUMN,
  approved metric dependency, dimension, concept and filter-rule records become
  a `networkx.MultiDiGraph`. FK constraint groups preserve composite order and direction.
- `SemanticGraphStore.save/load/network`: JSON only, atomic replacement, private
  temporary files, builder version, source ID, schema/catalog/profile identity,
  timestamp, content checksum and edge provenance. Cached networks are bounded to 32 datasources.
  Missing, corrupt, incompatible or stale snapshots fail closed to the existing path.
  No sampled values or result rows are stored; approved filter literals are metadata.
- `resolve_joins`: permitted live tables only; declared FK and current approved
  catalog paths, separate inferred suggestions, forbidden-join exclusion,
  composite mappings, provenance/cardinality ranking and ambiguity reporting.
  Default limits: 6 edges deep, 64 examined states, 50 ms. Paths are not proof
  that a join is analytically appropriate. Many-to-many/unknown grain remains risky.
- `resolve_metrics`: approved IDs/labels only; source fields, mandatory filters,
  compatible catalog version, formula inputs and bounded dependency closure.
  Conflicting labels, unsupported dependencies and stale facts are not promoted.
  Exact approved formulas and required columns enter `SemanticContract` and the
  generation context. Required joins come from approved relationships; optional
  dimensions are not automatically made mandatory query joins.

The injected semantic payload is limited to 12,000 characters. Approval notes
are omitted; approved formulas/filters are never truncated. Oversized required
facts return a structured context failure before SQL generation. Changed/stale
accepted metric definitions likewise stop generation rather than silently
reusing or replacing the accepted formula.

Approved catalog relationships enter context using `APPROVED_CATALOG`, not a fake
declared FK. This internal channel is a private request attribute, not client input.
The graph never edits the semantic catalog or the autonomous KPI registry.

## Enable, refresh and offline CLI

```ini
TEXT2SQL_GRAPH_ENABLED=false
TEXT2SQL_GRAPH_ROOT=graphs
TEXT2SQL_SEMANTIC_CATALOG_ROOT=data/semantic_catalogs
TEXT2SQL_ONTOLOGY_RECOVERY_ENABLED=false
```

Enable graph grounding explicitly after review. The application compiles metadata
at startup for registered datasource IDs (schema inspection only; no row sampling).
It does not compile graphs per question. Metadata changes invalidate a snapshot;
restart or explicitly rebuild after updating schema, profile, catalog or glossary.
Missing snapshots do not trigger on-demand construction. Graph-off retains stable
legacy bridge tie selection; graph-on rejects equally ranked paths instead.

For exported `SchemaInfo` JSON and optional existing catalog/profile/glossary files:

```bash
uv run python -m semantic_text2sql.graph_cli build --schema schema.json --catalog catalog.json --root graphs
uv run python -m semantic_text2sql.graph_cli validate --schema schema.json --catalog catalog.json --root graphs
```

Use the same optional `--profile` and `--glossary` arguments for build and validation.
The CLI never connects to the database. All graph/KPI/ontology operations are
local/offline by construction. This is **not** a claim that the whole application
is air-gapped: SQL inference and existing dense retrieval retain their own model
and network configuration. Generated graph files are gitignored.

## Optional ontology recovery

`OntologyAdapter` defines the read-only lookup boundary. The implemented adapter
reads only a compatible local graph and approved catalog, scopes results to the
recovery datasource and allowed tables, and returns compact definitions plus
separately labeled concept suggestions. Syntax, safety, provider timeout and
ordinary zero-row errors do not enable this tool. It uses the existing tool-call,
model-call and recovery-deadline limits, with no business DB query.

`METRIC_DEFINITION` claims must match the exact approved metric ID, source and
formula in the cited observation. They cannot prove that a category exists or
that a join caused NULLs. Automatic metric repair additionally requires a
provable AST mismatch; otherwise the outcome is escalation, not a semantic rewrite.
Relationship definitions describe joins, not live join effects. Inferred concepts
cannot authorize automatic repairs. No NVIDIA client or network provider is
enabled; optional external integration and provider timeout testing are deferred
pending a real API contract, credentials and measured benefit.

## Baseline, measurement and remaining verification

`benchmarks/results/debit_card_model1_ab_20.json` contains the previously reported
13 jointly failing cases (1481, 1482, 1526, 1531, 1533, 1472, 1473, 1480, 1490,
1501, 1529, 1500, 1525), gold SQL and generated candidates. The stored comparison
reports 6/20 result matches for each path and all queries accepted. These are
historical numbers, not a new evaluation or independently adjudicated business truth.

`benchmarks/benchmark_semantic_graph.py` records retrieval/join/aggregate/filter
AST-difference signals for each failure. They are explicitly **unadjudicated**:
different SQL shapes do not establish the causal failing stage. No evidence
establishes provider, validation or recovery root causes for every case.

Reproduce the local 101-run, three-table microbenchmark:

```bash
uv run python benchmarks/benchmark_semantic_graph.py --output benchmarks/results/semantic_graph_offline.json --root /tmp/queryflow-graph-benchmark
```

The JSON report records cold/warm load, build and traversal p50/p95 and zero
model/DB calls for these local operations. Safety/ambiguity checks add CPU overhead
over the original BFS. This is not an end-to-end speedup, SQL accuracy improvement,
token-saving estimate or a live recovery benchmark. Actual inference tokens,
fresh debit-card business/join accuracy, recovery success/false repairs,
PostgreSQL and NVIDIA integration remain NOT RUN. Metadata cannot prove arbitrary
formula equivalence, population/grain correctness or causality.

### Verified local checks (2026-11-04)

- Full Python suite: **704 passed, 2 skipped**, including 26 new cases.
- Frontend/voice: **198 passed**. Ruff, mypy (70 source files), lockfile validation
  and `git diff --check` passed. The context inspector distinguishes approved
  catalog relationships from declared FKs. No dashboard code was modified.
- Real SQLite service tests retain one SQL-generation call with graph disabled,
  enabled with a missing snapshot, and enabled with a valid snapshot. A stale
  accepted definition is rejected without another generation call.
- Recovery test: two scripted reasoning calls, zero DB probes, exact approved
  metric evidence. This is not a live-provider cost or false-repair benchmark.

Three-table offline fixture, 101 repetitions per operation, milliseconds:

| Operation | p50 | p95 |
|---|---:|---:|
| Original BFS reference | 0.0008 | 0.0010 |
| Shared bounded resolver | 0.0249 | 0.0294 |
| Snapshot-backed resolver | 0.0226 | 0.0246 |
| Resolver + local ontology relationship lookup | 0.0835 | 0.0882 |
| Warm snapshot load | 0.0296 | 0.0405 |
| Cold snapshot load | 0.0814 | 0.1069 |
| Offline compilation | 0.0886 | 0.0951 |

Each operation makes zero model and business-database calls. Added governance
costs more CPU than the original BFS; no speedup is claimed. The raw reproducible
report is `benchmarks/results/semantic_graph_offline.json`.
