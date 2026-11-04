# Graph amendment — Phase 0 gate (2026-11-04)

**STOPPED after Phase 0.** This amendment changes the implementation order.
Only evaluation scripts, annotations, a prospective split, tests and these notes
were added. Production orchestration, flags, catalogs and recovery are unchanged.
No NVIDIA work, catalog authoring, commit, push or application restart occurred.

## 1. Measured evidence and limits

Sources: `benchmarks/results/debit_card_model1_ab_20.json`, the original comparator
in `benchmarks/compare_model1_debit20.py`, the history fixture and its builder.
The reproducible report is `benchmarks/results/semantic_graph_phase0.json`.
Reproduce locally with the optional existing database root:

```bash
uv run python benchmarks/phase0_semantic_review.py --output benchmarks/results/semantic_graph_phase0.json --database-root ../bird-bench/llm/mini_dev_data/minidev/MINIDEV/dev_databases
```

Without `--database-root`, it performs artifact analysis only and explicitly marks
execution checks NOT RUN. No API key, DSN, raw DB rows or database files are saved.

### MEASURED — saved SQL re-execution

The debit-card DB is absent from repository `data/`, but **exists outside it** at
the configured/historical benchmark location. The amendment's presumed missing
database does not block this local execution recheck. API keys are present;
their validity/model availability was not tested, and no provider was called.

All 60 saved statements (20 references, 40 predictions) passed existing read-only
SQL validation and executed through `DatabaseRegistry.execute`, each with a
5-second deadline and a 500-row bound. Fifty-nine results were complete; case 1500's
reference exceeded the bound. Its two prediction mismatches are still provable:
each prediction is complete with <=500 rows, whereas the reference has >=501.
The report labels this proof explicitly instead of comparing truncated previews.

The original comparator rounds floats to six decimals, compares **all output
columns**, preserves multiplicity and treats any ORDER BY in the reference AST
(including nested ORDER BY) as order-sensitive. It is not a business oracle.

Raw counts below are identical in the saved labels and current SQL recheck:

| Cohort | n | Both match | Retrieval only | Model 1 only | Neither | Exact McNemar p |
|---|---:|---:|---:|---:|---:|---:|
| All | 20 | 5 | 1 | 1 | 13 | 1.0 |
| Prospective dev | 10 | 3 | 1 | 1 | 5 | 1.0 |
| Prospective heldout | 10 | 2 | 0 | 0 | 8 | 1.0 |

Only two overall pairs are discordant. There is no evidence of an accuracy
difference here; the heldout cohort has no discordant pairs and supplies no such
information. This does not establish equivalence or adequate statistical power.
These compare **retrieval vs Model 1**, not graph-on vs graph-off.

### MEASURED — exact table/ON agreement using SQLGlot

The evaluator records physical tables, explicitly joined tables, normalized ON
conjuncts, join kinds and unresolved lineage. It resolves lexical aliases and
direct CTE column projections, normalizes identifier case and symmetric equality,
and flattens AND parentheses. It does not guess aggregate/window lineage.
Exact agreement requires physical/joined-table sets and ON-predicate sets to
match. Join kinds are reported separately, not graded by this set comparison.

| Path | Exact structural agreement | Different | Unresolved |
|---|---:|---:|---:|
| Retrieval | 14 | 6 | 0 |
| Model 1 | 11 | 8 | 1 |

On the 19 mutually resolvable pairs: both match=11, retrieval-only=3,
Model-1-only=0, neither=5; exact McNemar p=0.25. Dev: 7/2/0/1 (p=0.5);
heldout resolvable: 4/1/0/4 (p=1.0), with one heldout pair unresolved.
**Structural disagreement is not a proven bad join:** EXISTS, deduplication and
preaggregation can legitimately differ from the reference. General business join
correctness/fanout appropriateness remains UNRESOLVED, not inferred from these counts.

### NOT RUN / not measurable from saved artifacts

- Fresh SQL generation, current end-to-end model latency, actual model-call/retry
  counts and input/output tokens: no new paired provider run was performed.
  Credentials exist, but reachability/validity was not tested at this gate.
- Recovery success and false repairs: no recovery traces/repaired SQL are retained.
  `false_repairs.count=null`, status NOT_RUN, **not zero**.
- Independent business correctness: questions, reference SQL and metric definitions
  conflict in several cases and need owner adjudication.
- Graph-on accuracy versus baseline: no paired graph-on predictions exist.
- NVIDIA integration: deliberately not started.

Historical recorded API latency is available, not rerun: retrieval p50=12,399 ms,
p95=31,615 ms; Model 1 p50=21,279 ms, p95=37,113 ms. These are old full-request
timings, not current provider timings or the new SQL-only execution durations.

## 2. Failure taxonomy with case IDs

There are **13 jointly mismatching cases**, plus two single-path mismatches (1479,
1498). Categories overlap; static evidence does not locate every causal stage.
The JSON annotations are evaluation-only, never runtime question-routing rules.

| Category | Cases / conclusion |
|---|---|
| Retrieval | **UNRESOLVED** for all 15 affected case IDs below. Only selected counts were retained, not candidate IDs, context or model inputs. Missing tables in final SQL cannot establish retrieval failure. |
| Join/source structure | Differences in 1481, 1480, 1500, 1501, 1525, 1526, 1529, 1531, 1533; Model 1 lineage for 1481 is unresolved. This is not nine proven join bugs. |
| Metric semantics/grain | 1473, 1481, 1482, 1490, 1525, 1526, 1529, 1531, 1533 show different formulas, populations, minimum scopes, denominators or duplicate policy. |
| Filters/time | 1480, 1482, 1500, 1501, 1525, 1526, 1529. Some are formatting/reference conflicts; Model 1's day-to-month substitution in 1525 clearly loses the requested daily restriction. |
| SQL generation/output | 1472, 1479, 1480, 1482, 1498, 1500, 1531, 1533 differ in output width, format, ranking or duplicate policy. All saved SQL parses; no syntax-generation failure is demonstrated. |
| Validation | All 28 reference-mismatching predictions were accepted in the stored run. This is an acceptance/reference gap, not proof that SQL safety failed or every prediction is business-wrong. |
| Recovery | **NOT RUN/UNRESOLVED** for all 15 affected IDs: no retained traces. Do not attribute failures or zero false repairs to recovery. |

Case-level observations for the 13 joint mismatches:

| ID | Specific evidence / required adjudication |
|---|---|
| 1481 | Global minimum raw Consumption vs per-segment minima of annual aggregates; different denominators. Define “least” and average grain. |
| 1482 | Reference omits EUR and returns three scalars; predictions filter EUR and return ranked segment rows. Reference/question conflict. |
| 1526 | Question says paid 634.8; reference uses Price=1513.12; predictions use Amount=634.8. Both a reference literal conflict and money/quantity confusion. |
| 1531 | Reference selects a high-consumption customer and SUMs unit prices; predictions rank spending and AVG unit prices. Do not codify the reference as an approved average-spending metric. |
| 1533 | Raw join multiplicity vs customer deduplication/EXISTS; predictions return IDs too, retrieval aggregates Consumption. Define customer/output grain first. |
| 1472 | Same basic annual ranking logic, but predictions add Consumption and a tie-breaker. Strict output shape can explain rejection; actual tie correctness is not inferred. |
| 1473 | AVG(Consumption)/12 vs average monthly totals vs customer-month average. Definition needed. |
| 1480 | MM vs YYYYMM plus an extra consumption column; EXISTS changes join syntax. |
| 1490 | Customer-month row percentage vs customer totals vs any-month distinct customers. Different denominators. |
| 1501 | Reference filters yearmonth; predictions filter transaction event dates. Membership in a consumption month does not mean a transaction occurred that month. |
| 1529 | Reference multiplies transactions across customer-month rows; predictions avoid fanout and filter transaction dates. Reference-grain risk. |
| 1500 | Reference uses consumption-month membership and duplicate descriptions; predictions use event time and DISTINCT. Truncation bound confirms mismatch, not business error. |
| 1525 | Reference is transaction-weighted; retrieval is distinct-customer weighted. Model 1 loses the explicit day. |

The discordant cases are largely output-shape differences: 1479 adds consumption
only on Model 1; 1498 adds month only on retrieval. A graph alone cannot resolve
these grading/definition issues.

## 3. Leakage controls and larger fixtures

`benchmarks/data/debit20_phase0_split.json` freezes a label-independent SHA-256
partition **before any new catalog authoring**:

- Dev: 1471, 1472, 1479, 1480, 1482, 1498, 1506, 1521, 1531, 1533.
- Heldout: 1473, 1476, 1481, 1490, 1500, 1501, 1509, 1525, 1526, 1529.
- Newly authored catalog case IDs: **[]**. No debit-card catalog was authored;
  only market_data exists in the repository catalog directory.

This is **prospective, not pristine**: all 20 gold queries were inspected during
earlier work, before the amendment. Sixteen questions overlap the history corpus;
eight are in this heldout cohort (1473, 1476, 1490, 1500, 1501, 1509, 1525, 1529).
Corpus overlap is a leakage risk, not proof those examples were included in each
historical prompt. Earlier glossary authoring case provenance is UNKNOWN.

The misleadingly named `bird_history_seed42_400.json` contains **399** SQL-labeled
records across 11 DBs. Its builder copies dataset SQL and sets `success=true`;
that flag is not execution or business validation. It contains no paired model
predictions. It could support a larger *development* run after label/provenance
review, but it is already a retrieval corpus, not a clean independent test set.
Other stored paired fixtures are Spider-20 and a one-case smoke run, not a larger
paired BIRD set. The historical 500-row source path in the comparator is missing.

Future catalog authoring must be limited to dev questions and approved schema/
profile/business documentation, never heldout evidence text or gold SQL. Existing
reference conflicts also prevent blindly copying **dev** gold into definitions.
Future evaluation must exclude heldout question/SQL duplicates from every history
channel (including schema-evidence retrieval), and preferably use fresh questions
unexposed to this work. The current source split cannot retroactively prove secrecy.

## 4. Largest-schema measurements; scope of kept code

**MEASURED**, 101 repetitions, largest of 11 inspected local BIRD SQLite schemas
by column count: european_football_2, 7 tables, 199 columns, 31 FK mappings;
84,024-byte snapshot. No row sampling, model calls or catalog authoring. This is
live-schema metadata only, without an approved football catalog/profile/glossary.
The compiler includes the current typed snapshot/checksum envelope, not a future
minimal in-memory-only implementation.

| Operation | p50 ms | p95 ms (nearest rank) |
|---|---:|---:|
| Current compile + NetworkX construction | 3.068 | 3.181 |
| NetworkX construction from compiled metadata | 0.211 | 0.215 |
| Cold JSON snapshot load | 2.020 | 2.160 |
| Warm snapshot load | 0.794 | 0.806 |
| First-five-table traversal | 0.377 | 0.385 |

Traversal seeds are Country, League, Match, Player and Player_Attributes.
The traversal terminates **BUDGET_EXHAUSTED**, not CONNECTED: these timings are
the cost of bounded unsuccessful traversal. They are not successful join-planning
latency. The 64-state cap/path variants require investigation in Phase 3.
These millisecond measurements do not justify **mandatory** persistence or prove
an end-to-end improvement; in-memory traversal/caching should be evaluated first.
Cold/warm refer to the Python snapshot-store cache; the OS file cache was not
purged. Warm load includes snapshot copying, not just a cached graph pointer read.

| Existing implementation kept | Current gate / amendment conflict |
|---|---|
| NetworkX dependency | Already explicitly declared; current lock contains it. At local pre-graph checkpoint `7375d8f`, `uv.lock` contained no NetworkX entry, even transitively. No dependency change needed now. |
| Shared resolver | Kept. Public wrappers default to legacy shortest path with graph disabled. Graph-on currently rejects ambiguity rather than returning baseline path plus typed candidate warnings: **pending Phase 3 change**. |
| KPI grounding | Kept behind `TEXT2SQL_GRAPH_ENABLED=false` by default. No debit-card definitions authored. Phase 4 must follow adjudicated dev definitions. |
| Local ontology | Kept behind `TEXT2SQL_ONTOLOGY_RECOVERY_ENABLED=false`. Typed formula evidence exists; amendment-specific applicability/repair restrictions and filter/join claims need the later Phase 6 review. |
| Durable JSON store/CLI/startup builder | Kept. Parent graph flag defaults OFF, but a separate default-OFF persistence flag and request-local in-memory path are **not yet implemented**. Deferred at this explicit Phase 0 stop gate. |
| NVIDIA adapter implementation | None started; local protocol remains only an optional extension boundary. |

### KPI sources — no merging or replacement

- `BusinessGlossary`: curated term/alias/formula context and existing structural
  formulas. It enriches SQL context; its terms are not automatically equivalent
  to human-approved catalog records or evidence that database values exist.
- `MetricCatalogEntry` in `SemanticCatalogStore`: formal, versioned governed
  definitions; current `approved` records with compatible live support are the
  authority for registered SQL metrics. New service grounding is opt-in; disabling
  it does not magically unify all older glossary/catalog paths. Conflicting
  definitions require explicit handling, never silent merging.
- `autonomous_analytics.KPIRegistry`: separate monitoring/investigation definitions
  and metric relationships. It neither replaces the catalog nor automatically
  authorizes SQL formulas. The existing SemanticContract carries resolved SQL
  requirements; no second KPI source was created in this amendment.

## 5. Gate outcome and verification

Supported next work: Phase 3 bounded resolver/typed ambiguity warnings while
preserving baseline behavior; output-contract and daily-time regressions; then
Phase 4 only from adjudicated approved dev definitions. Local ontology follows.
Durable persistence remains optional; do not start NVIDIA integration.

Tests cover alias/CTE normalization, composite predicates, wrong keys/schema,
unresolved lineage, exact McNemar, split integrity, unknown taxonomy stages,
history overlap, and actual SQLite rejection of writes/truncation-bound proofs.
Full check results are recorded in V6_4_PROGRESS.md. Node is not installed here;
the existing Deno CJS runner is used instead. No later runtime work proceeds
until this Phase 0 report is reviewed.
