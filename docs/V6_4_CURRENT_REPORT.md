# V6.4 implementation checkpoint — 2026-11-04

Baseline: local `bb2846f`; working-tree continuation, not a remote-branch reset.
Phase 0 findings are accepted unchanged. No held-out catalog authoring, NVIDIA
integration, new agent, dependency, database migration, server restart or publish.

**This is not a declaration that all V6.4 requirements are complete.** Supported
semantic proofs were extended; unknown semantics remain UNRESOLVED. The remaining
acceptance gaps below must not be hidden behind a green regression suite.

## Implemented source changes

- `semantic_lineage.py` (new): scope-aware physical expression resolution through
  direct projections/CTEs, output aliases and grouping ordinals. Unused CTEs cannot
  supply proof. Expansion is bounded by depth 24 / 4,096 nodes; quoted,
  set-operation and otherwise unsupported lineage returns unknown.
- `validator.py`: final-output alias checks; final grouping expressions rather
  than column occurrence; additional grouping keys require dependency proof;
  physical aggregate inputs including distinct counts; matching approved derived
  expressions; reversed comparisons, singleton IN equivalence, exact membership,
  ordered BETWEEN and conjunctive range equivalents. Negated/disjunctive or
  collation-dependent matches are not asserted correct. Symbolic periods without
  explicit boundaries remain UNRESOLVED. Composite equijoins require complete
  mappings for multiplicity proofs, using declared PKs or supported preaggregation,
  never sampled uniqueness. Complex join chains remain unproven.
- `join_resolver.py`: graph-on best-first path search with dominance pruning and
  at most two equal optimal candidates. Connect required seeds without enumerating
  every pair/path variant. Existing graph-off BFS/tie behavior remains. Typed
  ambiguity candidates retain table paths, relationship IDs, rank and provenance;
  an ambiguous result has no authoritative selected relationships. The 64-state,
  six-edge and 50ms bounds were not increased.
- `semantic_graph.py`, `api.py`, `service.py`: independently default-OFF runtime
  JSON persistence; bounded in-memory snapshots/frozen NetworkX graphs. Cached
  network identity/checksum is checked before use; allowed schema/tables/fields and
  forbidden edges are still filtered per request. Returned edges are defensive
  copies. Standalone explicit save/CLI compatibility remains.
- `graph_grounding.py`, `models.py`: approved metric version/unit/definition
  identities, including entity and relationship metadata. Changed accepted
  definitions, conflicting labels, oversized selections and incompatible required
  joins cannot silently replace approved facts. Filter conflicts include equality,
  membership and inequality contradictions. Only approved table time semantics
  are promoted. The three existing KPI/glossary stores remain separate.
- `service.py`, `.env.example`: independent opt-in KPI grounding without graph
  traversal; already-accepted approved metrics are always revalidated. Correctness
  recovery definitions enter the governed contract and undergo current-catalog
  validation before context building/generation. Mid-turn catalog drift fails
  before another SQL generation call.
- `ontology.py`, `recovery.py`: current datasource/schema/catalog version and
  allowed-table evidence binding; exact claim-to-definition validation; recovery
  deadline checked before and after lookup. Unsupported ontology repairs escalate.
  Definitions do not prove live values or join effects. The tool is local/read-only.
- `service.py`, `models.py`: failed post-result repair attempts and usage survive
  into the request ledger. Repair execution outcome is distinct from correctness.
  `false_repair=true` is recorded only for a proven semantic FAIL; unassessed
  business correctness remains null, never a fabricated zero false-repair rate.
- `conversation.py`: category comparisons widen the accepted filter instead of
  becoming comparison periods; removal of one membership preserves the others;
  year changes retain explicit day restrictions and reject impossible dates;
  approved aggregation changes require metric review; unaffected qualified filter
  and grain dependencies survive metric changes.
- `conversational_bridge.py`, `conversational_agent.py`: discovery uses the same
  current-catalog resolver; stale/incompatible definitions are omitted rather than
  advertised. Schema inspection is bounded/offloaded and metered in the existing
  request budget and ledger. Opaque definition digests stay out of routine LLM
  context (`context.py` and Top-layer serialization), but remain in backend state.

Model 1 and the separate plotting model were already removed in the baseline;
they remain removed. Principal conversational roles are Top-layer, SQL generation
and conditional recovery, and may share one underlying provider/model. The
existing dashboard designer and explicitly disabled-by-default legacy modes were
not rebuilt. No additional autonomous agent or intent router was introduced.

## V64 acceptance status

Initial means the prior matrix's final status. VERIFIED is local acceptance for
the stated criterion, not production readiness or proof for every possible SQL.

| ID | Initial → final | Changed code / passing evidence | Remaining gap |
| --- | --- | --- | --- |
| 01 | IN_PROGRESS → IN_PROGRESS | scoped grouping/aliases/ordinals/CTEs; `test_semantic_completion.py`, `test_semantic_validation.py` | General transformed lineage and functional-dependency equivalence |
| 02 | IN_PROGRESS → IN_PROGRESS | physical measure/approved derived AST checks; same tests | General alternate algebra, casts and multi-stage aggregation equivalence |
| 03 | IN_PROGRESS → IN_PROGRESS | exact operators/values, reversed predicates, IN/BETWEEN, unused scopes; same tests | General Boolean equivalence, collation and correlated predicates |
| 04 | IN_PROGRESS → IN_PROGRESS | direct CTE boundaries, daily-preserving changes; semantic/grounding tests | Formal symbolic-period and storage-aware date arithmetic proof |
| 05 | IN_PROGRESS → IN_PROGRESS | complete composite keys, PK/preaggregate multiplicity, distinct entity count tests | General multi-join cardinality / live population evidence |
| 06 | IN_PROGRESS → IN_PROGRESS | output-lineage DISTINCT, ranking retained, approved unit/NULLIF formula and drift tests | General NULL policy, unit conversion and ranking equivalence |
| 07 | IN_PROGRESS → IN_PROGRESS | category comparison/member removal/day preservation; `test_remaining_multiturn.py`, conversation/grounding tests | Full cross-field delta conflict model beyond supported operations |
| 08 | IN_PROGRESS → VERIFIED | frozen TurnContext/stable IDs retained; first-turn, same-turn refine, history and chart-only tests in V63/SQL-memory/integrated suites | Production multi-worker deployment not claimed |
| 09 | IN_PROGRESS → VERIFIED | delayed version-checked commits retained; concurrent/cancelled/history tests; failed-repair audit regression | External identity ownership remains 20 |
| 10 | IN_PROGRESS → VERIFIED | bounded SQL→refine→chart / requery / result-tool / failed-tool integrations retained and passing | Unbounded autonomous investigation is deliberately unsupported |
| 11 | IN_PROGRESS → VERIFIED | reset, historical branching, restart, cancellation and source-isolation regressions retained and passing | Real login-bound ownership remains 20 |
| 12 | VERIFIED → VERIFIED | both-flags Top-only routing regression unchanged | Explicit legacy compatibility path retained |
| 13 | VERIFIED → VERIFIED | verified snapshot/no-second-inspect tests unchanged; existing SQLite mtime cache reused | No redundant new schema cache |
| 14 | IN_PROGRESS → VERIFIED | shared approved metric resolver/discovery; stale/unit/version/scope and drift tests | Glossary and monitoring registry intentionally remain separate |
| 15 | IN_PROGRESS → IN_PROGRESS | deterministic chart service/proposal/sufficiency/source-ID tests unchanged | Legacy non-Top presentation path not fully consolidated |
| 16 | IN_PROGRESS → IN_PROGRESS | existing task-specific provider and truncation/fallback tests remain passing | Exhaustive adapter/live-provider verification not run |
| 17 | IN_PROGRESS → IN_PROGRESS | failed repair attempt/token retention and catalog inspection budget/ledger fixed | Full per-transport recovery timing/retry ledger audit remains |
| 18 | VERIFIED → VERIFIED | rejected Plotly promise activates ECharts in frontend regression | Pixel/device QA not run |
| 19 | IN_PROGRESS → IN_PROGRESS | result-ID DOM/cache/history/chart tests remain passing | Full real-browser multi-turn identity matrix not run |
| 20 | BLOCKED → BLOCKED | existing session/database/allowlist/SQL safety tests unchanged | Legitimate deployment identity, session ownership and tenant policy absent |
| 21 | IN_PROGRESS → IN_PROGRESS | new real SQLite monthly→yearly and comparison→percentage tests, adversarial semantics and repair tests | Exact all-artifact assertions for all original 32 scenarios remain incomplete |
| 22 | BLOCKED → BLOCKED | optional PostgreSQL tests skipped; no live provider call | Test PostgreSQL DSNs and authorized live-provider verification required |

Final totals: **8 VERIFIED, 12 IN_PROGRESS, 2 BLOCKED**. No requirement was removed
or counted NOT_APPLICABLE to conceal a gap.

## Original 32-scenario coverage inventory

1–2, 12–17, 19–22, 24, 27, 29–31: existing `test_v63_conversation.py`,
`test_sql_memory.py`, `test_integrated_chart_planning.py`, provider/runtime tests.
3–4: new `test_remaining_multiturn.py`, real SQLite results, SQL/state equality,
version/identity/call-count and successful tool-order assertions.
5–6: conversation, grounding and multi-turn tests.
7–11: semantic suites; supported positive/negative/adversarial AST proofs, not a
universal independent business oracle. 23: the dedicated plotting provider no
longer exists; invalid/missing Top proposals exercise deterministic fallback.
25–26: frontend explanation/clarification and rejected-Promise fallback tests.
28: catalog/discovery/grounding tests. 32: session/database/reset isolation is
tested, but real authenticated ownership remains BLOCKED. This inventory is not
a claim that every original per-scenario artifact assertion is complete.

## Measurements

### Paired scripted HTTP/SQLite benchmark (MEASURED)

Same `test_single_planner_paths.py -k benchmark` against an isolated archive of
`bb2846f` and the working source; 10 measured requests/scenario after warmup,
scripted providers, real SQLite. Model counts, DB operation counts and fixture
result/chart validity are unchanged; zero retired stage calls in both versions.

| Scenario | p50 ms before → after | p95 ms before → after | LLM calls | DB operations |
| --- | --- | --- | --- | --- |
| SQL | 32.603 → 32.463 | 35.543 → 74.176 | 2 → 2 | 2 → 2 |
| SQL + chart | 32.906 → 32.935 | 35.071 → 34.871 | 2 → 2 | 2 → 2 |
| Cached plot | 22.766 → 22.887 | 24.362 → 23.856 | 1 → 1 | 0 → 0 |
| Chart type | 22.962 → 23.164 | 24.714 → 24.455 | 1 → 1 | 0 → 0 |

All 40 measured requests in each version satisfy the known `[5,30]` fixture
results and requested valid chart/source checks. This is fixture correctness,
not held-out SQL or independently adjudicated business accuracy. No meaningful
latency speedup is established; SQL p95 regressed in this small sample. Scripted
Top usage remains fixed 10 input/5 output tokens; SQL stub usage is unknown.
Actual live input/output tokens and independent chart quality: NOT RUN.
Catalog-definition lookup now has one explicitly metered schema operation when
a catalog is present; its existing mtime cache is reused. No business-row scan.

### Largest-schema metadata microbenchmark (MEASURED)

`benchmark_grounding_completion.py`: 101 repetitions, existing local
european_football_2 metadata, 7 tables/199 columns. No provider or business-row
queries. Phase 0 baseline is not rerun or relabeled as fresh inference.

| Operation | Current p50 / p95 ms |
| --- | --- |
| Compile + NetworkX build | 3.049 / 3.284 |
| In-memory snapshot defensive copy | 0.771 / 0.801 |
| Cached frozen NetworkX access | 0.000167 / 0.000208 |
| Snapshot-based bounded traversal | 0.410 / 0.449 |
| Cached-network bounded traversal | 0.428 / 0.474 |

Prior traversal: BUDGET_EXHAUSTED at the 64-state cap (Phase 0 p50/p95
0.377/0.385ms). Current traversal: **AMBIGUOUS, 14 states, one typed warning
containing two candidate paths**. This is better diagnosis, not proof of a
unique correct join or an end-to-end speedup. Cached traversal was not faster in
this run. The seven-table dense regression also connects within the unchanged cap.

Live generation/result-match accuracy, independently adjudicated semantic accuracy,
real-provider token/latency comparisons, production recovery success/false-repair
rates, PostgreSQL, and browser pixel/device QA: **NOT RUN**. Synthetic false-repair
tests prove instrumentation, not a production rate. External authentication and
sharing verification: **BLOCKED** by the prerequisites in row 20.

## Continuation checkpoint

Executed final checks: `uv run --offline pytest -q -o addopts=''` (765 passed,
2 PostgreSQL skips, 19.72s); `uv run --offline ruff check src tests` plus the new
benchmark; `uv run --offline mypy src` (71 source files); `deno test -A tests/*.cjs
tests/voice_input_test.mjs` (198 passed, 629ms); `git diff --check`. UV used an
isolated writable cache. Provider/PostgreSQL/live-browser verification was not
substituted with mocks and called live; those boundaries remain explicitly unrun.

New tests: `test_semantic_completion.py`, `test_grounding_completion.py`,
`test_remaining_recovery.py`, `test_remaining_multiturn.py`; existing semantic and
graph tests were updated for stronger/current behavior. No tests were deleted.
Benchmark: `benchmarks/benchmark_grounding_completion.py`. No migration is needed:
new persisted contract/recovery JSON fields are optional/backward-compatible;
existing SQLite tables, history/dashboard revisions and credentials are unchanged.

Next uncompleted requirement: **V64-01**, general final-grain/functional-dependency
proof beyond the supported scope. Then V64-02/03/04 multi-stage lineage, Boolean
and temporal normalization, and the remaining rows above. Keep unsupported cases
UNRESOLVED; do not manufacture business definitions from the contaminated holdout.
Preserve this uncommitted checkpoint. No `.env`, database or screenshots changed.
