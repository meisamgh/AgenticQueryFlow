# Offline graph-enhanced semantic grounding

The metadata-only [Semantic Explorer](SEMANTIC_EXPLORER.md) exposes bounded schema,
approved KPI lineage and potential impact views over this same snapshot.

**Current defaults (2026-11-08):** graph persistence, KPI grounding, KPI-first
retrieval and local ontology recovery are enabled at the user's request. Explicit
false still opts out. Earlier [Phase 0 findings](SEMANTIC_GRAPH_PHASE0.md) describe
historical gates, not the current default settings.

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
TEXT2SQL_GRAPH_ENABLED=true
TEXT2SQL_GRAPH_PERSISTENCE_ENABLED=true
TEXT2SQL_KPI_GROUNDING_ENABLED=true
TEXT2SQL_KPI_FIRST_ENABLED=true
TEXT2SQL_GRAPH_ROOT=graphs
TEXT2SQL_SEMANTIC_CATALOG_ROOT=data/semantic_catalogs
TEXT2SQL_ONTOLOGY_RECOVERY_ENABLED=true
```

The application compiles metadata
at startup for registered datasource IDs (schema inspection only; no row sampling).
Metadata changes invalidate a snapshot. KPI-first requests with approved metric
facts can rebuild a missing compatible snapshot from the already verified metadata;
other requests use the startup or explicit CLI snapshot. Graph-off retains stable
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
cannot authorize automatic repairs. No external ontology client or network provider is
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
PostgreSQL and external ontology integration remain NOT RUN. Metadata cannot prove arbitrary
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

<a id="kpi-first-grounding-opt-in"></a>

## KPI-first grounding (default-on)

Set `TEXT2SQL_GRAPH_ENABLED=true` and `TEXT2SQL_KPI_FIRST_ENABLED=true`.
Both flags default to true. Existing exact-name grounding, explicit graph-off behavior,
catalog-free databases and v1 LightGBM feature vectors remain compatible.
`TEXT2SQL_KPI_GROUNDING_ENABLED` and the independent local recovery flag retain
their existing roles. On 2026-11-08 the five semantic flags in the local .env were
enabled at the user's request; provider/model/key settings were not changed.

The existing approved metric entry accepts optional `aliases`, for example:

```json
{
  "id": "net_revenue",
  "label": "Net revenue",
  "aliases": ["sales after refunds"],
  "formula": "SUM(amount) - SUM(refund_amount)",
  "required_fields": ["amount", "refund_amount"],
  "dependencies": ["gross_sales", "refunds"],
  "dimensions": ["departments.name"]
}
```

This is a fragment, not a complete catalog: source relation, units, time grains,
human approval and quality assertions must already be supplied and validated.
Aliases inherit the approved definition; no machine creates an approval record.
Glossary synonyms are used only when their term anchors to that approved identity
and the glossary belongs to the current datasource. Alias collisions stop before
SQL. Unresolved expressions use existing BM25/local embeddings and RRF to return
bounded, explicitly nonauthoritative candidates; similarity never certifies a KPI.

`resolve_metrics` remains the single dependency-expansion owner. It reports cycles,
missing/unapproved inputs, drift and unsupported formulas; the 16-metric closure
bound is unchanged. KPI-first mode rejects cross-relation composite metric
dependencies: the current executable formula contract cannot establish their
calculation grain. It does support a single-source metric grouped through
multiple authorized relations and bridge tables. Textual KPI names inside formulas
are never treated as physical columns.

Before hybrid ranking, mandatory metric/filter/dimension columns and source tables
are pinned. Unrelated optional tables no longer force joins; optional ranking
evidence remains available. The existing NetworkX snapshot/compiled network and
join resolver preserve full composite keys and approved versus declared provenance.
Missing snapshots are compiled from the same verified metadata and remembered in
the existing store; its persistence policy is unchanged. AMBIGUOUS, DISCONNECTED
and BUDGET_EXHAUSTED mandatory plans stop before generation, not partial success.

The typed `SemanticContract.kpi_grounding` carries identity/provenance, dependency
chains/distances, mandatory columns/tables, requested/supported dimensions, source
grain, aggregation/rollup rules, allowed time grains and selected relationships.
Approved formulas/versions remain in `approved_metrics` and compact semantic
facts. The generator receives both through existing verified context; formulas and
required facts are rejected if over budget, never silently sliced. Dependency KPIs
do not require separate output columns.

Existing SQLGlot validation is extended for missing metric inputs, required grouping
and complete key mappings. Direct simple aggregate-ratio mismatches fail; complex
formula rewrites, unsupported time grains and unproven multiplicity remain
UNRESOLVED. SQL execution is not proof of semantic correctness. Algebraic/CTE
equivalence, general fanout proof and cross-fact aggregation are not implemented.

### Optional ML compatibility

The original 11-feature v1 model is unchanged. `kpi_candidate_features` supplies
five pre-join features in `KPI_RERANKER_FEATURES_V2`; post-join count/risk diagnostics
are separate and never fed backwards into candidate ranking. Loading v2 explicitly
requires a `.features.json` companion with `schema: "kpi_v2"`, the exact ordered
feature list, and matching model feature names/count. No v2 artifact or labeled
training set is supplied, and v2 is not activated in environment configuration.
Mandatory dependencies are pinned regardless of ML scores.

### Reproduce and measured results

```bash
uv run pytest -q tests/test_kpi_first.py
uv run python benchmarks/benchmark_kpi_first.py
```

The real SQLite service test resolves “sales after refunds by department”, retains
the complete-status filter and gross/refund dependencies, discovers the customer
bridge, and returns synthetic department A / 135.0. Exactly one scripted SQL-model
call and no resolution/planner call occur. No real business rows or external model
are involved.

Paired **synthetic pre-final retrieval/grounding** benchmark: five identical
questions, one excluded warmup pass, 250 observations per mode, deliberately tight
one-table/one-column optional retrieval, and a scripted dense encoder:

| Measurement | Baseline | KPI-first |
|---|---:|---:|
| Grounding p50 / p95, ms | 0.207 / 0.431 | 0.423 / 0.539 |
| Synthetic root identity accuracy | 60% | 100% |
| Required-column recall at retrieval | 20% | 100% |
| Complete dependency recall | 60% | 100% |
| Required join-plan table coverage | 20% | 100% |
| Timed SQL / LLM calls / LLM tokens | 0 / 0 / 0 | 0 / 0 / 0 |

This measures additional deterministic work, not a speedup or final-system SQL
accuracy. Baseline later context verification may recover exact-match columns;
these figures refer to retrieval before that stage. Join coverage is not an
independent semantic adjudication. Full model-generated SQL accuracy, live encoder
latency, independent business correctness, production data and PostgreSQL/provider
integration are NOT RUN. Raw results: `benchmarks/results/kpi_first_offline.json`.

Changed runtime: semantic_catalog.py, models.py, kpi_resolution.py,
graph_grounding.py, hybrid_retrieval.py, service.py, context.py, llm.py, validator.py.
Tests: test_kpi_first.py; existing graph/catalog/semantic/reranker suites retained.
No new graph engine, metric registry, autonomous agent, dependency or external API.

Final local checks: 892 Python passed, 2 dedicated PostgreSQL tests skipped;
211 frontend/voice passed; Ruff, mypy src / configured mypy (73 source files)
and git diff --check passed. The KPI-first acceptance file contains 36 cases.
