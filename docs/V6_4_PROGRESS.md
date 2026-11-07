# V6.4 progress checkpoint

## 2026-11-07 — SQLite connection/inspection failure boundaries

Five tests failed before fixes: a malformed SQLite file escaped as sqlite3.Error,
the schema API raised instead of returning JSON, connection failures leaked raw
driver errors, failed read-only PRAGMA initialization did not close its handle,
and file-stat failure escaped while retaining a cached schema. connect now
translates driver failures to DATABASE_CONNECTION_FAILED and closes partially
initialized connections. inspect translates sqlite3/OSError failures to
DATABASE_INSPECTION_FAILED and evicts that database's cache entry. Existing
DatabaseError codes (including identity/authorization and connection failures)
are preserved. Programming exceptions are not broadly swallowed.

Source: database.py. test_sqlite_failures.py covers these boundaries plus
generation stopping with database_error and zero model calls on a corrupt schema.
The existing API DatabaseError handler returns safe JSON using its pre-existing
diagnostic map; no raw driver path or result data enters the client response.
Targeted schema/URI/WAL/agent tests: 25 passed (0.25s). Full Python suite:
856 passed / 2 PostgreSQL skipped (35.55s). Frontend/voice: 211 passed (783ms).
Ruff, mypy src and configured bare mypy (72 files), and git diff --check pass.
No live remote-engine/provider or browser verification, no speedup claimed.
No dependency, SQL rewriting, fallback to previous data, agent or extra model
call. No commit, push, restart, real business-data or live-provider use.

Audit remains active. Next candidate: verify cache invalidation on same-mtime
atomic file replacement, then audit per-query row/time bounds in registry callers.
Broader semantic and production-identity gates remain open.

## 2026-11-07 — Committed SQLite WAL visibility and schema consistency

Two real-SQLite reproductions failed before fixes: execution missed a committed
row still in WAL, and schema inspection returned a cached pre-ALTER schema when
only WAL changed. Read-only connections no longer declare changing databases
immutable. mode=ro and query_only remain enforced. The existing schema cache
uses main-file and optional WAL stat identity/size/timestamps, rather than adding
another cache or querying the schema on every cache hit. A read transaction
keeps multi-statement schema/key inspection internally coherent.

test_sqlite_wal.py verifies committed visibility versus uncommitted data,
write rejection even with query_only disabled, warm schema reuse, WAL-only
schema invalidation, and a concurrent ALTER during schema inspection with a
consistent old snapshot followed by the new schema. Targeted suite: 48 passed
(0.36s) before the additional coherent-snapshot test; all three WAL tests then
passed (0.03s). The first full run had 849 passed / 2 skipped and one flaky
exact-SQL comparison failure: the existing timed optimizer selected equivalent
reordered conjunctions. test_remaining_multiturn.py now isolates generator/state
identity from the optional optimizer via a scoped fixture, preserving all exact
SQL/contract/result assertions. Dedicated optimizer tests remain active. Combined
WAL/multiturn/optimizer tests: 8 passed (0.79s). Final full Python suite:
850 passed / 2 PostgreSQL skipped (36.91s). Frontend/voice: 211 passed (781ms).
Ruff, mypy src and configured bare mypy (72 files), and git diff --check pass.
Existing URI-path tests retain
coverage for encoded file identity and mode=ro.

Read-only WAL readers require SQLite's normal WAL/shared-memory access; no
unsafe immutable fallback is used if that infrastructure is unavailable. No
credentials, actual business DB, LLM calls, dependencies, commit, push or restart.
No production latency improvement claimed. Audit and broader semantic/security
gates remain open; next candidate is file-replacement/cache identity and
structured handling of SQLite connection/inspection failures.

## 2026-11-07 — SQLite filesystem/URI identity boundary

Four regressions failed before the fix: roots containing #, ? or percent-encoded
text opened incorrectly, and a root with a # fragment read a different synthetic
database outside the intended resolved file. DatabaseRegistry.connect now uses
Path.as_uri() before appending the existing read-only/immutable URI options.
No filename fragments can replace those options. Existing root containment,
database ID validation and query-only enforcement remain unchanged.

test_sqlite_uri_paths.py covers six real SQLite cases: reserved characters,
spaces, Unicode and a decoy database. Cases verify exact row identity and schema
inspection; disabling query_only still cannot write through mode=ro. Targeted
URI/result/agent verification: 30 passed (0.25s); six URI tests also
passed after adding explicit inspection assertions. Full Python suite:
847 passed / 2 PostgreSQL skipped (34.52s). Frontend/voice: 211 passed (787ms).
Ruff, mypy src, configured bare mypy (72 files) and git diff --check pass.
No live remote-engine/provider or browser QA, no speedup claimed.
No SQL text, LLM calls, dependency, credential or analytical transformation was
added. No commit, push or restart; prior worktree changes preserved.

The audit remains active. Next candidate: determine whether immutable SQLite
connections can ignore legitimate uncheckpointed WAL data; reproduce before
changing the existing immutable policy or schema-cache invalidation.

## 2026-11-07 — Non-finite result serialization failure

Synthetic SQLite SELECT 1e999 returns float infinity, which the prior adapter
passed through unchanged; Starlette JSONResponse then raises ValueError. Ten
negative regression cases failed before fixes (real SQLite execution plus three
float forms across SQLite/PostgreSQL/remote serializers). The shared row
serializer now rejects non-finite floats with SQL_RESULT_NOT_JSON_SAFE, retaining
ordinary finite numbers, NULL, booleans and text unchanged. No conversion to
invented NULL/zero values. PostgreSQL and remote adapters reuse the same guard.
The API error mapping supplies a safe diagnostic without SQL/result values.

Existing bounded generation repair catches the DatabaseError: acceptance remains
false on exhausted attempts, and finite repaired SQL may succeed. No extra agent,
retry policy, SQL/model call or serialization dependency. Tests:
test_nonfinite_results.py and two generation regressions in test_agent.py.
Targeted tests: 24 passed (0.27s). Final full Python suite: 841 passed /
2 PostgreSQL skipped (32.98s). Ruff, mypy src and configured bare mypy pass
(72 files); git diff --check passes. Frontend/voice: 211 passed (797ms).
Live PostgreSQL/remote engines, external
providers and browser QA were not run. No latency improvement claimed.

Scope: non-finite float values returned within the existing row bound; existing
string serialization of Decimal/dates and exact historical snapshot behavior
are unchanged. Broader semantic and production-identity requirements remain open.
No commit, push or restart. Audit remains active.
Continuation candidate: verify SQLite URI handling for configured root paths
containing URI-reserved characters; reproduce before changing connection logic.

## 2026-11-07 — Profile-save integrity and containment

Reproduced four failing regressions before fixing ProfileStore.save: partial
writes destroyed the existing JSON, destination symlinks were followed outside
the store, crafted IDs escaped the directory, and a concurrent writer could
attach its timestamp to another version's cached object. ProfileStore now uses
direct-child filename checks, rejects destination symlinks, writes and fsyncs a
same-directory temporary file, then publishes with atomic replacement. A saved
entry is evicted rather than cached with a potentially unrelated timestamp.
Temporary files are cleaned on failure; concurrent trusted writers remain
last-successful-replacement-wins, not revision-locked. This is not a claim of
protection against hostile local directory replacement or power-loss durability.

Source: profiling.py, sidecar_store.py. Acceptance tests:
test_profile_save_boundaries.py (five scenarios, including replacement failure).
The full Python suite passed 824 tests / 2 PostgreSQL skipped (39.27s). The
additional replacement-failure test was then verified with all five save tests
(5 passed, 0.04s); no runtime source changed after the full run. Frontend/voice:
211 passed (879ms). Ruff, mypy src and configured bare mypy pass (72 files),
and git diff --check passes. No live DB/provider or browser QA claimed. No SQL, LLM,
dependency, analytical behavior, or production-authorization changes. No commit,
push or restart. Broader audit remains active; next candidate is unsupported
non-finite query results at JSON serialization boundaries, not yet reproduced.

## 2026-11-07 — Profile engine identity and verification-gate repair

Reproduced two metadata-boundary failures: a postgres-named JSON file containing
a sqlite profile was accepted, and compile_graph accepted that profile against a
PostgreSQL schema with the same database ID. Both tests failed before fixes.
ProfileStore now checks the embedded dialect on cache/load returns, and graph
compilation independently rejects dialect mismatch. Database IDs and existing
path/symlink controls remain unchanged. Source: profiling.py / semantic_graph.py;
regressions: test_profile_dialect_boundary.py.

Repaired pre-existing configured checks: use Python 3.12 generic syntax in the
shared sidecar loader; distinct tuple-key loop variables in reset_session_caches;
sort imports in the two affected test modules. No type ignores or validator
weakening. Targeted metadata/reset/graph suite: 39 passed. Ruff src/tests passed;
mypy src passed (72 files); frontend/voice 211 passed (815ms); diff check passed.
Full Python verification: 820 passed, 2 PostgreSQL skipped (37.64s).

Preserved pending graph-default and retired-model cleanup changes. No commit,
push, restart, actual .env, credentials, business data, model stage, dependency
or agent change. No live-provider, real PostgreSQL or browser QA claimed.

Continuation candidate: inspect atomicity and containment of metadata save paths;
do not change them without a concrete reproduction. Broader V6.4/production gates
are not promoted by these local tests. Earlier Phase 0 stop notes are historical;
the active user goal explicitly authorizes this continued audit.

## 2026-11-07 — Retired model and vendor-reference cleanup

Removed the obsolete OpenRouter model registration and vendor-specific references
from README, tests, benchmark code/results and local documentation. Adapter tests
now use a neutral synthetic model ID while retaining wire-format, token, missing
key and truncation coverage. The API catalog no longer offers that retired option.
Deleted one obsolete local .env comment; active provider/model values and secrets
were not changed. Local graph/ontology functionality remains independent.

Case-insensitive search is clean across src, web, tests, benchmarks, README,
.env.example, .env and docs. The ignored imported-conversation archive and existing
Git history were not rewritten. Targeted provider/API suite: 68 passed (2.32s);
changed-file Ruff, benchmark JSON parse and whitespace checks passed. Full Python
suite: 818 passed / 2 PostgreSQL skipped (40.87s). No commit, push or restart.

## 2026-11-07 — NetworkX default enabled; external ontology absence verified

User explicitly requested graph-on as the default. Changed fallback defaults in
API startup, service, linker and hybrid retrieval to true; .env.example and
README agree. Explicit false still opts out, graph persistence remains false,
and optional LOCAL ontology recovery remains false. No external ontology service,
SDK or endpoint is integrated. The obsolete remote model option and vendor
references were subsequently removed. Actual .env has no graph override. No restart,
commit or push performed.

Added missing-variable service/approved-metric and bounded bridge regressions.
Full Python suite: 818 passed, 2 PostgreSQL skipped (39.56s); frontend/voice:
211 passed (756ms); changed-file Ruff and diff checks pass. Repository-wide
checks expose pre-existing UP047 in sidecar_store.py, two import-order findings
in tests, and two mypy errors in the unchanged reset_session_caches helper
(api.py lines 198–199). These were not introduced by the graph-default change
and remain outside this scoped request. No live provider/DB or browser claims.

## 2026-11-04 — Amendment Phase 0 stop gate

Only evaluation/docs/tests changed; no new runtime work or debit-card catalog
authoring. Saved-artifact SQLGlot table/ON comparison, case annotations, a frozen
prospective 10/10 split, raw paired counts/exact McNemar and read-only saved-SQL
rechecks are in SEMANTIC_GRAPH_PHASE0.md and semantic_graph_phase0.json.
DB files do exist outside repository data/; provider keys are present but no
provider validity/generation/recovery request was made. False repairs remain
NOT_RUN/null, not zero. No raw result rows, secrets or database files were saved.

Largest inspected schema: european_football_2, 7 tables/199 columns/31 FKs. Timings
measure the retained compiler, NetworkX build, snapshot loads and bounded traversal;
the last returns BUDGET_EXHAUSTED, not a successful join plan. Default graph and
ontology flags remain OFF. A distinct persistence-OFF flag and requested ambiguity
warnings are pending AFTER this stop gate; existing working code was not deleted.

The history fixture has 399 SQL-labeled records, 16/20 question overlaps; 8/10
prospective heldout questions overlap it. All 20 gold queries were previously
inspected, so this is not a pristine holdout. Authoring IDs remain empty. Source
definitions, output shape and time/population conflicts require adjudication
before treating exact reference matches as business correctness. No external ontology work,
commit/push/restart or changes to unrelated images/environment were performed.

Phase 0 verification: 716 Python tests passed, 2 PostgreSQL integrations skipped;
198 frontend/voice tests passed with Deno (Node unavailable). Executed uv run
pytest/ruff/mypy with an offline temporary UV cache; pytest addopts override made
the summary visible. Ruff src/tests and the two new benchmark scripts, mypy
(70 source files), and diff whitespace checks passed. Twelve new evaluation tests
cover lexical alias/CTE handling, wrong keys/qualification, unprovable lineage,
McNemar, prospective split integrity/leakage disclosure, and actual read-only SQL
rejection/truncation proofs. No later runtime phase is marked VERIFIED.

## 2026-11-04 — Offline graph-enhanced semantic grounding

Checkpoint before implementation: `7375d8f`. Added deterministic NetworkX
compilation, versioned atomic JSON snapshots and a bounded cache; unified three
join traversals with permission checks, composite mappings, forbidden relationship
handling and ambiguity reporting. Graph-off keeps legacy tie selection. Optional
service grounding propagates approved catalog metrics, mandatory filters and
dependency columns; catalog joins retain distinct APPROVED_CATALOG provenance.
The existing single Top-layer agent, SQL service, SQLGlot safety, result state and
plotting paths remain. No dashboard code was changed.

Optional local query_ontology reuses the recovery tool loop, existing deadlines
and call limits. Exact typed metric evidence is checked; definitions alone do not
authorize semantic repair without a provable SQL mismatch. No external ontology
provider is enabled. Graph building occurs at explicit CLI refresh/startup, never
per question. See SEMANTIC_GRAPH.md for interfaces, flags and the offline CLI.

The stored debit-card benchmark includes 13 paired failures. AST-difference
signals are saved as UNADJUDICATED, not invented root-cause labels. New performance
results are synthetic local build/load/traversal measurements with zero model/DB
calls; they are not a measured end-to-end speedup or SQL-accuracy improvement.
Fresh debit-card accuracy, real-provider token use, PostgreSQL and external ontology tests
remain NOT RUN. Production identity constraints and older incomplete V6.4 items
are unchanged. Local verification results are recorded in the delivery report.

Final local checks: 704 Python tests passed, 2 PostgreSQL integration tests skipped
without their configured DSNs; 198 frontend/voice tests passed. Ruff, mypy
(70 source files), lockfile consistency and diff whitespace checks passed.
Twenty-six new Python cases cover graph/KPI/ontology behavior, including Model 1
pruning, long-path graph-off compatibility, stale accepted metric rejection,
checksum corruption, exact qualified sources and bounded context. Recovery shares
the service metadata stores/cache. The inspector labels approved catalog joins
separately from declared FKs. Actual .env and the unrelated workflow image were
not changed; no commit, push or server restart was performed.

## 2026-11-03 — Compact thinking experience

Frontend waiting uses an animated leaf, one event-backed activity label,
discreet elapsed time and observed-step summary instead of speculative badges.
The collapsible event log is rebuilt only when events change. Existing polling,
cancellation and immediate response handoff are unchanged. Reduced-motion and
mobile styles are included. No backend/model/SQL calls added. Verification:
196 passing frontend/voice tests, including transitions, unchanged-event updates,
completion, failure, cancellation and accessibility markup. Browser visual QA,
live-provider and mobile-device verification were NOT RUN.

## 2026-11-02 — Dashboard failure diagnostics and annotation correction

Confirmed retained failures: rationale exceeded 600 characters, then the retry
hit the combined all-digits/markup/finding annotation rule. Old logs did not
identify the second field or individual trigger. Added safe structured failure
records (code/field/repairability/exception/opaque IDs/attempt), per-attempt logging,
API/Studio propagation, current-message frontend precedence and useful errors.
No raw model output, SQL rows, credentials or unrecognized schema field names are logged.

The prompt targets one short rationale under 300 chars; the existing hard 600
limit remains. Grounded years/labels are allowed, but unsupported claims/markup
still fail. Per-chart annotation permissions are source-scoped. Candidate, axis,
mode, source and revision checks remain. Repair gets specific safe feedback and
whitelisted prior choices; unrelated valid selections cannot be redesigned during
repair. At most one repair; provider/request/deadline failures do not trigger it.

Acceptance includes real SQLite/API creation → failed edit → save-original checks,
log redaction, years, invalid IDs/axes, mode/stale refs, failed repair preservation
and provider non-retry. Full verification: 677 Python passed/2 skipped (23.53s),
189 frontend/voice passed (509ms), Ruff/mypy (65 source files)/diff passed.
No live-provider/PostgreSQL/browser pixel checks, restart, .env changes or commit/push.
Changed designer, models, API, conversational agent, Studio, frontend message
selection/cache tag, diagnostics tests and docs. Prior uncommitted work preserved.
Final additional source-scoping test confirms a year present in one retained
analysis cannot authorize the same label on another source's chart. Final run:
678 Python passed/2 skipped (22.58s), 189 frontend/voice passed (509ms).

## 2026-11-01 — Ordered chart layers and single Primary

Applied the user's refinement: filter decisions first, then KPIs, then visible
Primary/Comparison/Distribution/Relationship chart layers. This supersedes earlier
internal-perspective-only UI guidance. Proposal schema/catalog ordering now puts
filter selection before KPIs/charts. Added optional analytical_role metadata for
backward-compatible documents; new AI designs require role assignments and one
Primary. Edits preserve roles, and removing the sole Primary requires a promotion.
Validation, restore and source snapshots retain existing safety/lineage boundaries.

Frontend renders the four layers in order, every chart on its own grid row and
KPIs permitted in a shared row. Empty unsupported layers say no verified plot was
selected; no chart/data is fabricated. Older unlabelled dashboards remain
compatible rather than being automatically assigned new analytical meanings.
No extra model/SQL calls or new agents. The running server was not restarted.

Tests cover strict new-design roles, unique Primary/order, PATCH preservation and
promotion, role persistence across restart, four-layer UI order and empty states.
659 Python passed/2 skipped (23.21s); 188 frontend/voice passed (511ms),
Ruff/mypy/diff passed. No live provider or browser pixel review for this refinement;
no .env changes, commit or push. Restart/refresh and redesign to adopt new layouts.

## 2026-11-01 — Intelligent three-layer dashboard design and editing

Read the supplied Intelligent BI Dashboard Design & Editing task and reused the
existing Top-layer agent, specialist, candidate catalogs, PATCH materializer and
renderers. Reworked specialist prompt into verified filters/scope, eligible KPIs,
then complementary visualizations. Primary/comparison/distribution/relationship
are internal perspectives, not mandatory sections or a chart/KPI slot quota.

Extended existing profiles with finite-only quartiles, mean, population deviation,
IQR outliers, category concentration and up to six paired correlations from four
non-identifier numeric columns. No samples or categorical literals were added.
Evidence remains per-result, at returned SQL grain, descriptive/noncausal. Added
KPI definitions/period metadata and previous widget meaning to the compact context.
No new model/database calls, agents or independent SQL execution paths.

Explicit edit instructions separate actual scope/metric changes from metadata,
style/layout or encoding edits. PATCH preserves unmentioned components; conflicting
explicit order positions now fail validation. AI chart widths are honored with
each plot anchored in a separate grid row; deterministic defaults remain full
width and mobile rendering stays bounded. Existing API/schema limits are retained.

New acceptance tests cover behavior-sensitive scripted selections under identical
schemas, finite/missing/constant statistics, filter/KPI eligibility, stable widget
replacement IDs, removal/reorder/width, per-source evidence and renderer sizing.
Existing saved/legacy/multi-turn/read-only/repair tests remain passing.
652 Python passed/2 skipped (21.47s), 187 frontend/voice passed (509ms),
Ruff/mypy (65 source files)/diff checks passed. Local context-only benchmark:
0.0268 → 0.0468 ms median, 2,453 → 3,321 JSON bytes (101 runs, four-row fixture).
This is added bounded evidence cost, not a production latency/token improvement.
No live provider or browser pixel QA for this change; no server restart, .env
changes, commit or push. Details: INTELLIGENT_DASHBOARD_DESIGN.md.

## 2026-11-01 — Bare dashboard request inherits accepted analysis

Confirmed the latest user request "design a dashboard" followed an accepted
consumption-by-date result in the same session. The model unnecessarily asked for
the complete objective/metrics/filters again. Top-layer instructions now resolve
that follow-up from the accepted question/contract/result. Compact context falls
back to the accepted cache's question, columns and semantic contract if the
separate conversation state is absent. Cached references still pass existing
scope and completeness checks. No second classifier or blanket default metric.

Added a regression exercising bare dashboard creation with a cached accepted
analysis, no ConversationState, no SQL calls, one Top-layer call, unchanged source
identity and preserved comparison periods. The dashboard handler is simulated;
existing designer/multi-result/Studio integration tests remain in the full suite.
Live two-turn check: initial query accepted 21 rows; dashboard routing initially
filled source_run_id incorrectly, and the bounded correction hit Groq HTTP 429.
Added explicit field/prompt instructions to use source_result_ids for dashboards
and null source_run_id/operation. Full live dashboard success is NOT VERIFIED.
Existing port 8001 and its accepted state remain untouched; no provider/key changes.

Checks: 647 Python passed/2 skipped (22.04s), Ruff/mypy (65 source files) passed.

## 2026-11-01 — Groq strict-schema rejection corrected

Latest run failed before tools with HTTP 400 invalid_request_error. A minimal
live request confirmed Groq rejected proposal_version/minimum after the scoped
schema restricted that inactive legacy field to enum=[null]. This was an invalid
wire-schema constraint, not a missing key or unavailable model. Null-only inactive
proposal ID/version/approval quote now use plain null schemas without irrelevant
string/numeric bounds. The canonical Pydantic constraints remain unchanged.
No validation weakening, plain-JSON workaround, provider/model or .env edits.

Live verification: minimal Groq native-schema request accepted; isolated full
consumption-by-date API request accepted, 21 rows, no dashboard, one Groq Top-layer
call and one Groq SQL-generation call, 2.40s. No history writes to the running
8001 server and no restart, honoring the user's request to restart it personally.
Regression: scoped inactive legacy fields are null-only while canonical minimum
remains 1; existing provider schema and native-grammar suites remain covered.
Checks: 646 Python passed/2 skipped (21.95s), 186 frontend/voice passed (508ms),
Ruff/mypy/diff checks passed. No commit or push.

## 2026-11-01 — Native artifact routing fixes blocked query path

Latest persisted failure: Top Llama returned a completion/dashboard-goal conflict
twice, so SQL never ran. A completion-only grammar experiment avoided that pair
but allowed an ongoing false dashboard goal and repeated SQL. Both failed tests
are retained as diagnostic evidence, not reported as successful query execution.

Implemented typed optional result_kind on the existing AgentDecision, with native
Ollama branches decoded artifact-first. Ordinary analysis/chart calls cannot set
dashboard_goal or invoke dashboard tools. Dashboard workflow remains supported;
legacy responses without result_kind parse compatibly. Existing canonical scope
validation and bounded repair remain, with no second router or autonomous agent.

Full isolated live API check using actual configured providers and debit-card DB:
accepted total consumption grouped/ordered by Date, 21 rows, one Top-layer call,
one SQL-generation call, no dashboard calls/retries, 16.34s. In-memory test history
only; the user explicitly declined restart of the current 8001 server, which was
left running unchanged. Reload is needed before that process uses the fix.
Checks: 646 Python passed/2 skipped (23.57s), 186 frontend/voice passed (597ms),
Ruff, mypy (65 source files), diff checks passed. No commit/push or .env changes.

## 2026-11-01 — Plain analytical question incorrectly escalated to dashboard

Read the latest local run for "Total consumption over date": governed SQL
succeeded, then the Top layer invoked the dashboard designer. Optional future
analyses caused a requery; later designer responses stopped at 2,048 output tokens
and failed JSON parsing. No stored results were edited during diagnosis.

Added a leading SQL-only example and explicit output-scope prompt rules: earlier
dashboards do not authorize a new dashboard for every later metric question.
Added deterministic decision consistency validation: an ask_data decision cannot
simultaneously finish after SQL and start a dashboard goal. The existing one-repair
path fixes this before tool execution; no competing intent router was added.
Optional further_analyses are now suggestions rather than mandatory requery;
essential missing_requirements still block incomplete designs. Designer output
instructions are more compact; completion limits/providers remain unchanged.

Isolated live local Llama check initially exposed the contradictory dashboard_goal.
After the guard, it returned ask_data / NEW_QUERY / dashboard_goal=null /
finish_after_tool=true in two model calls (one repair), zero DB calls, 8.66s.
No actual SQL, session writes or dashboard build ran in this live routing check.
The existing 8001 server was not restarted, preserving its accepted RAM state.
Regression evidence: ordinary trend with prior dashboard context, contradictory
scope repair before SQL, optional future suggestions, multi-source dashboard flow.
Final checks: 645 Python passed, 2 skipped (23.53s); 186 frontend/voice passed
(591ms); Ruff, mypy (65 source files), and diff checks passed. No commit/push.

Local KPI comparison-bubble continuation: removed the visible Understood-as strip
and its response-render call. Each KPI now has a neutral value-side comparison
bubble. Existing PERIOD_CHANGE cards use exact retained baseline/current evidence,
formula validation, known units and chronological observations. Other summary KPIs
show No comparison rather than borrowing an unrelated period growth rate. No
unsupported min/max comparisons, inferred polarity, model or SQL calls. Per-card
container queries wrap the bubble at narrow card widths without changing the grid.
Tests cover comparison evidence and absent extrema baselines. Browser visual
verification remains unavailable.

Treemap/chart-change continuation: added native Plotly flat treemap tiles with
one unique ID/value per source row, and equivalent ECharts fallback; negative,
missing, or nonfinite values are rejected. Non-additive metrics are not newly
offered treemap. Forwarded KPI highlight methods through the shared chart handle.
Added reusable ChartChangeBubble with 36px neutral pill, verified percentage and
48x24 chronological microchart, plus exact values/period/formula tooltip. Existing
comparison eligibility remains strict; absent approved metric-polarity metadata
means neutral styling, never green/red based on sign alone. KPI icon bubbles are
44px with 20px semantic SVGs and matched shadows. Frontend/KPI/bubble tests: 104
passed. Synthetic 500-iteration mock-DOM build/render measured p50 0.037ms and p95
0.045ms, zero network/model/database calls; this is not real-browser render timing.
Full Python suite passed on rerun (one skip); an earlier run hit the pre-existing
timing-sensitive optimization assertion, which passed in isolation. Browser visual
verification remains unavailable. No SQL generation or backend calculations changed.

KPI/icon detail refinement: deterministic metric/calculation icon selection with
neutral fallback; green/amber/violet 40px outlined/shadowed bubbles; consistent
36px forest-green assistant leaf avatar. Chart title now receives a neutral,
result-ID-bound period-change badge only from existing valid comparison evidence.
Periods are formatted from known temporal granularity. Missing, zero, and unsupported
negative baselines remain suppressed; no business-performance polarity inferred.
No KPI calculations, SQL execution, or model calls were added for these UI changes.
KPI/frontend tests: 99 passed. Responsive CSS is preserved; connected-browser visual
validation remains unavailable.

Targeted frontend refinements: mint bordered user bubbles, consistent circular
leaf assistant/loading avatars, conditional chart tab (Data default without a
validated chart), metadata-aware compact number displays with raw values kept for
sorting/CSV, em-dash NULL display, comparison badges in KPI headings, and exact-value
hover text. Plotly range slider disabled, smaller bottom margin, white marker
outlines, and formatted hover text. Sidebar Clear styled with trash icon and
confirmation before any reset/deletion request. Regression verifies cancellation
causes zero API calls and absent charts cannot expose Visualization. Browser
desktop/mobile visual inspection remains unavailable; no backend logic changed.

KPI-only premium visual refinement: reusable icon bubbles and semantic calculation
accents (green primary, amber MIN, violet MAX), compact locale-aware K/M formatting,
right-aligned verified sparklines with extrema markers, and evidence-derived context
badges. Exact raw values/calculations remain in hover and accessible labels. Min/max
cards target exact Plotly source points via curve/point hover, without SQL/model
calls or source-row mutations. Existing calculation eligibility, placement, result
identities, and responsive grid are preserved. Added number/unit/locale, evidence
badge, DOM, and exact point-targeting tests. Connected-browser visual verification
is still unavailable; responsive CSS/DOM tests are not screenshot verification.

KPI completion continuation: cards consume explicit ChartSpec measure units and
accepted approved-metric/aggregation definitions. Additive totals require an exact
approved SUM input, matching SUM stage grouped by the selected temporal dimension,
unique periods, complete source rows, and no contract LIMIT. Other metrics keep
descriptive row statistics. Sparklines use unique, validated temporal values in
chronological order. Growth is emitted only for two explicitly named contract
periods with exact source-row matches and a positive baseline; unavailable periods
and zero/negative baselines suppress growth. Grouped cards retain exact series
names for local Plotly/ECharts highlighting. Currency/percentage units are never
guessed from metric names. Added DOM/unit tests without network or SQL access.
Browser visual verification was attempted but no browser is connected; not verified.

Local visualization styling/KPI continuation: enlarged filled white leaf; area
line width 3 with top-to-bottom fading fill in Plotly/ECharts; zoom-guide text
and reset control hidden while built-in zoom is preserved. Added typed JSDoc KPI
specifications and deterministic complete-result row summaries below charts.
Only numeric non-NULL complete accepted evidence with a stable result ID is used;
no inferred totals, units, growth or comparison baselines. KPI calculation runs
once per response, outside styling rerenders. Minimum/maximum and unweighted
single-series row averages are descriptive statistics, not business-total claims.
No additional SQL or LLM calls. KPI/front-end suites: 87 passed; browser visual
verification remains outstanding. Optional comparative KPIs/sparklines are not
implemented when the accepted data lacks explicit supporting semantics.

Local green frontend continuation: QueryFlow/leaf branding, hidden database selector,
removed Home/data-source navigation, icon-triggered local history search, and user
questions beside avatars. Restored exclusive Visualization/Data/SQL views with
response-local tab and vertical-scroll state. Added retained-row search/sorting,
column visibility, modifier-click cell/row selection and clipboard copying, SQL
download/expansion, and expandable actual backend validation/execution/context
metadata. Raw values remain the CSV source; numeric display adds grouping without
changing source values. Observed value types appear in header tooltips (not claimed
database types). Partial results explicitly scope local search/sort to cached rows.
Verification: 83 frontend tests, full Python suite (one skip), Ruff, mypy, and diff
check passed. Live-provider/browser end-to-end interaction remains unverified.

October 26 results-table update (local, uncommitted):

- Data now has responsive column widths, sticky numeric-aligned headers, drag-to-resize columns, compact small-result sizing, 20/50/100-row pages, a bounded 100-row virtual window, and a full-screen table. The source result and CSV values remain unchanged; truncated CSV export is explicitly labeled as partial.
- The old Visible chart data subview and verbose source-row note are removed. A Map tab is offered only for an explicit map request with a complete result containing valid numeric latitude/longitude columns; it plots exact returned coordinates with Plotly.
- A new `/api/results/page` endpoint accepts only session/database/result identity and an offset/size, never client SQL. Beyond the accepted 500-row snapshot, it re-runs only the saved read-only SQL with an outer ORDER BY, checks the saved prefix for drift, and caps access at 10,000 rows / five seconds. This is bounded offset paging, not a cross-request database snapshot; concurrent source updates can still require rerunning the question. No SQL LIMIT is added or changed.

October 26 single-tool response and view update (local, uncommitted):

- The initial Top-layer decision can now explicitly mark a simple governed SQL result, or SQL plus its requested validated chart, as complete. Those successful paths return the tool's grounded response without a separate final-answer model call. Multi-step refinement, investigation, explanation, and failure handling retain the existing continuation path. The optimization is conditional on the model setting `finish_after_tool`; it is not a guarantee for all provider outputs.
- The frontend no longer has an Answer tab. The response text, KPI, and requested chart remain visible above the Data/SQL tabs; switching technical tabs does not run SQL or regenerate a chart.
- Scripted SQLite regressions verify one Top-layer call for both simple SQL and SQL-plus-chart when marked complete. Frontend tab tests verify the persistent chart area and local switching. Live-provider latency impact is not yet measured.

October 25 same-turn consistency continuation (local, uncommitted):

- Delegated SQL calls now receive the most recent accepted state through a request-private, request-local handoff. The second same-turn SQL call preserves the turn's original optimistic commit version; no intermediate state or result is persisted. API JSON cannot supply this handoff.
- Same-turn result tools (`analyze_result`, `explain_existing_result`, `present_result`) read the current graph response before the persisted cache. A failed second SQL call retains the first accepted result but cannot be reported as a successful refinement.
- Added SQLite end-to-end regressions for NEW_QUERY → REFINE → chart, SQL → each result tool, failed second SQL, historical branch replay, cancellation, and rejection of client-supplied provisional state. Date-bound cleanup now removes only the exact old-year upper boundary, ranking extraction retains multiword metric names, and explicit `fanout_risk` produces an UNRESOLVED finding for one-to-many aggregate joins.
- Full semantic equivalence and the remaining V6.4 scenario matrix are still incomplete; these tests do not establish business correctness for every SQL rewrite.

October 25 provider continuation (uncommitted):

- Live, credential-backed Groq probes used only a synthetic decision prompt: text, JSON-object and tiny strict schema returned HTTP 200. The full Queryflow AgentDecision instructions returned HTTP 400 `json_validate_failed` in strict-schema mode; the same full prompt succeeded in JSON-object mode and passed local Pydantic validation. This establishes a prompt/schema-size-dependent Groq/Qwen strict-decoding failure, not a missing model or invalid key.
- The existing per-model/schema strict-mode failure cache and bounded JSON-object fallback remain active. Groq 429 retries now consume the shared model-call budget; a specific budget-exhausted error is no longer mislabeled as a deadline. Safe failure logs include response format and retry count. `/api/ui-config` now exposes effective assistant, context, SQL and plotting model identities without credentials, and the frontend Pipeline inspector displays them.
- Added a separate True SOTA Responses adapter and opt-in model catalog entry from the prior user request. This is not evidence of live True SOTA connectivity; no `TRUE_SOTA_API_KEY` was configured. The local, untracked `.env` now selects Groq/Qwen for Top-layer, Model 1, SQL and plotting; True SOTA remains selectable by configuration.
- A live end-to-end debit-card `/api/chat` test was blocked by the environment's data-egress review because it would send database schema and profile information to Groq. No such live debit-card request was made. A separate live end-to-end test used only a temporary synthetic books database: the initial strict AgentDecision call failed with `json_validate_failed`, JSON-object fallback succeeded, Model 1 and SQL generation succeeded, and read-only execution returned fiction=2/history=1. The final client answer was correct for those synthetic rows. This does not establish accuracy on BIRD or PostgreSQL.

Baseline: `c2564d4` on `main`. Untracked `screenshots/` belongs to the user and is not part of V6.4 work.

Implemented in this checkpoint:

- Added provable SQL AST checks for direct GROUP BY dimensions, physical aggregate inputs, and required predicate operand/operator/value. Complex lineage stays `UNRESOLVED`; many-to-many aggregate joins surface a fanout warning rather than an unsupported rejection.
- Reconciled several contract edits: metric changes clear stale metric-dependent fields; conflicting equality filters are rejected; period and ranking updates preserve unrelated constraints.
- Passed the request's verified schema/profile snapshot into generation, avoiding a duplicate live inspection in the governed service path.
- Preserved a stable chart handle when Plotly rejects asynchronously and replaced the failed chart with ECharts. A DOM-mocked test rejects the actual `newPlot` promise.

Checks run: Python pytest full suite passed with one skipped test; 71 Deno frontend tests passed; Ruff, mypy over `src`, and `git diff --check` passed. No live-provider or PostgreSQL integration run was performed. These checks do not prove the entire V6.4 brief complete.

October 24 continuation (uncommitted):

- Added frozen request-local `TurnContext`, immutable SQL-result/chart IDs, deferred Top-layer conversation commit, same-turn SQL-to-chart source binding, and historical result restore. The SQL-to-plot and restore regression tests pass; this does not close every concurrency or cancellation scenario.
- Reproduced a Groq Qwen strict-schema `json_validate_failed` on a minimal live decision call. Implemented a bounded JSON-object fallback with local `AgentDecision` validation and a per-model/schema native-failure cache, so subsequent decisions avoid repeating the rejected native mode. An unrelated HTTP 400 remains terminal. Two minimal live JSON-mode decision calls succeeded; no live SQL generation/execution or PostgreSQL test was run.
- Added task-specific Groq completion budgets, safe typed HTTP failures, truncation detection, and tests for 400/401/403/404/429 failures, structured fallback, exact call ledger, and no SQL execution on invalid provider response.
- Verification after these changes: full Python pytest passed with one skipped test; 71 Deno frontend tests passed; Ruff and mypy passed. Remaining V6.4 rows and partial statuses are recorded in `V6_4_MATRIX.md`.
- Added direct temporal-boundary checks and final ORDER BY/LIMIT checks to the conservative semantic validator, with positive, negative, and unresolved CTE/window tests. This does not prove full date-expression or ranked-metric equivalence.
- Applied the data-sufficiency gate after fresh SQL execution as well as on saved results. First-turn SQL plus plotting remains supported; an adversarial fresh result that returns currency instead of requested country now yields `REQUERY_REQUIRED` without invoking the plotting model.
- Wired the existing semantic catalog into conversation metric discovery; only human-approved catalog metrics are exposed. A test verifies approved formula visibility and inferred metric exclusion. Glossary/catalog unification is still incomplete.
- Bound the frontend chart view to `source_result_id` when present; mismatched result identity no longer renders the chart. A JavaScript regression covers matching, mismatching, and pre-identity saved responses.
- Extended approved-metric validation for a narrow, provable case: a single direct aggregate over the catalog's physical input. Wrong direct inputs fail; casts, competing aggregates and derived formulas remain `UNRESOLVED` instead of being treated as proven.
- End-of-continuation checks: the full Python suite passed with one skipped test on rerun; 72 Deno frontend tests, Ruff, mypy, and `git diff --check` passed. An earlier full-suite run had one non-reproducing failure in the legacy conversation test; the isolated test and immediate full rerun both passed. No claim of V6.4 completion follows from these tests.
- Verified that the Top-layer assistant exclusively handles `/api/chat` when both Top and legacy bridge flags are enabled. The legacy path remains available only as a compatibility route when Top mode is off.
- Reconciled explicit date equality filters with a four-digit active period. A conflicting year now requires a comparison-period declaration or a clarified change; positive and negative tests pass.
- Added a Top-layer regression proving that final-answer provider failure after accepted SQL preserves that result, marks the answer unavailable in provenance, and accounts for the failed model call and reported tokens.
- An end-to-end saved-aggregate → `REQUERY_REQUIRED` → governed monthly SQL → validated line-chart test exposed a false HTTP 409. Deferred state commits now use the version accepted at turn start rather than the new SQL state's version; the complete sequence passes and preserves chart source identity.
- Added a concurrent same-session ASGI test: both requests reach SQL generation, then exactly one commits and the other receives HTTP 409. This verifies stale-write rejection in the current process; parent-run restore still writes intermediate state and needs further work.
- Latest verification: full Python pytest passes with one skipped test; 72 Deno frontend tests pass; Ruff, mypy over 55 source files, and `git diff --check` pass. PostgreSQL and a live end-to-end Qwen SQL workflow were not run.

Still open: full semantic equivalence (derived formulas, temporal bounds, ranking, units, proven join fanout), complete delta/state semantics, all plotting-source identity/cancellation cases, conversational tool sequence consolidation, production identity/authorization, and the requested complete SQLite scenario matrix. See `V6_4_MATRIX.md` for individual statuses.

To resume: read this file and `V6_4_MATRIX.md`; inspect `git status`; complete the next high-severity NOT_STARTED or IN_PROGRESS row. Do not reimplement already VERIFIED behavior. Run `uv run pytest -q`, `uv run ruff check src tests`, `uv run mypy src`, `deno test --allow-read tests/frontend_rendering.cjs tests/voice_input_test.mjs`, and `git diff --check` after code changes. Record live-provider/PostgreSQL tests separately as NOT RUN when unavailable.

October 25 conversation-hardening checkpoint (uncommitted):

- The Top-layer prompt now includes the accepted analytical contract's approved metrics, formulas, mandatory filters, period/comparison boundaries, grouping, ranking, measure and correction context. A prompt regression confirms key fields are actually sent.
- A stale `CHANGE_PERIOD.previous_value` is rejected without mutating the previous contract. Simple final ranking validates the requested ranked metric as well as direction/limit. Complex ranking still remains `UNRESOLVED`.
- `parent_run_id` no longer writes a historical snapshot into accepted state before executing the new request. Its state and complete cached result are request-local; a successful branch commits against the current live version. Failed SQL no longer persists a failure-state copy as accepted state. An unresolved-branch regression and the existing branch/restart integration test pass.
- SQL safety success with unresolved semantic findings is now labeled `SQL_SEMANTICS_UNRESOLVED`, not `SQL_SAFETY_VALID`; execution still does not prove business correctness.
- Verification: full Python pytest passed with one skipped test, 72 Deno frontend tests passed, Ruff passed, mypy passed over 55 source files. Live PostgreSQL/provider tests were NOT RUN. Full derived-formula/date equivalence, cancellation matrix and all 32 requested SQLite scenarios remain open; do not mark V6.4 complete.

October 25 PR #3 chart-follow-up integration (local, uncommitted):

- Reviewed the open GitHub PR `#3` and integrated its chart-follow-up behavior into the local working tree without merging the PR or overwriting the earlier uncommitted conversation fixes.
- The plotting model receives the prior validated chart encodings and Top-layer typed changes. A chart-type-only request cannot silently switch x, y, metric, components, or series; invalid proposals fall back to deterministic chart validation preserving the prior encodings. A period change still returns `REQUERY_REQUIRED` before the plotting model is called.
- Replaced the chart proposal prompt with the requested expert business-analytics visualization-planner instructions, retaining the application's JSON schema and bounded result sample.
- Verification: focused visualization/V6.3 tests, full Python pytest (one skipped), 72 Deno frontend tests, Ruff, mypy over 55 source files, and `git diff --check` passed. No live-provider call was made; improved chart quality in live use remains unmeasured.

October 25 Top-layer routing hardening (local, uncommitted):

- Clarified DIRECT_ANSWER, NEW_QUERY, MODIFY_QUERY, VISUALIZE, EXPLAIN, INVESTIGATE and result-reuse instructions in the existing Top-layer prompt. No additional routing model or graph was added.
- Reconciled validated `AgentDecision` proposals against accepted `SemanticContract`, result/chart availability and typed changes. Presentation-only chart edits reuse accepted data; typed analytical changes delegate to governed SQL. Exact result recaps use `present_result` and preserve result identity. Unsupported direct answers to obvious database questions are grounded through `ask_data`.
- A safe `REQUERY_REQUIRED` follow-up now preserves the accepted question and original chart goal, runs one governed refinement and retries charting without a redundant decision call. Fresh mismatched SQL results and repeated insufficient chart results terminate without inventing a chart. This does not prove general semantic equivalence or every ambiguous natural-language change.
- Adversarial SQLite multi-turn tests cover model misroutes, chart-type edits, filter correction, same-turn SQL/chart, result reuse, failed SQL, and a premature answer after a requery signal. Full Python pytest passed with one skipped test; 72 Deno frontend tests, Ruff, mypy over 55 source files, and `git diff --check` passed. Live provider and PostgreSQL tests were NOT RUN.

October 26 same-turn orchestration and performance audit (local, uncommitted):

- Confirmed the request-local provisional result/contract path through SQL → refine → explain and SQL → chart. Added regressions for latest-result use and explanation-only timing isolation. Final answers now guard against unresolved required-tool failures even if a later benign tool observation intervenes.
- The call ledger now records actual Top-layer decision/final-answer durations, task names, retry counts and provider/model; context/SQL planner entries carry provider retry counts, and SQL validation/execution are timed separately. Removed a phantom legacy-interpreter model entry in Top-layer mode. The pipeline panel uses request-local stage timings and per-call metadata, rather than inherited SQL timings on result-only turns.
- Task-specific completion limits and schema-constrained output paths were checked and extended across configured AgentRouter, True SOTA, Groq, JustDoWork, CodeCraft and OpenRouter adapters where their HTTP interfaces support them. The CLI fallback cannot guarantee a per-call completion cap; live-provider behavior remains unverified.
- Corrected a local `.env` typo (`groqre` → `groq`) that made the assistant unavailable. The missing-provider error now names the setting and available configured providers. Restart the API process to reload `.env`.
- Scripted SQLite measurement for SQL → explain: graph workflow 194 ms, SQL generation 1 ms, SQL execution 1 ms; rounded decision/final-answer/validation/explanation stages reported 0 ms. These are mock-model timings, not live-provider latency; graph total includes unclassified orchestration and test-client overhead.
- Verification: full Python pytest passed with one skipped test, 68 Deno frontend tests passed, Ruff passed, mypy passed over 55 source files, and `git diff --check` passed. Live providers and PostgreSQL were NOT RUN.

October 26 local Gemma understanding integration (uncommitted):

- Verified the installed Ollama model tag `gemma4:12b`; local `.env` now selects it for the Top-layer assistant. SQL and plotting configuration is unchanged.
- Added an Ollama conversation completer with task token limits, token accounting, bounded HTTP timeout and native JSON/schema output. The installed Ollama build rejects the complete AgentDecision grammar; a specific capability fallback uses JSON mode and existing local decision validation, remembering the rejected capability per model.
- A live local greeting produced a valid AgentDecision via fallback in 6.98 seconds, reporting 123 tokens. This verifies local completion and parsing, not analytical routing accuracy or production latency. Provider regression tests, full pytest (one skipped), Ruff and mypy passed.
# Local premium chart styling — 2026-10-29

## Executable remote adapters — 2026-10-30

- Added a shared DatabaseDialect type and lazy RemoteDatabaseRegistry, extending
  the existing non-SQLite injection point rather than introducing another pipeline.
- MySQL, SQL Server, Snowflake and BigQuery are registered from explicit environment
  manifests and reach existing schema/retrieval/generation/validation/execution,
  recovery, paging and metadata paths. SQL remains validated and schema-qualified.
- Added parameterized native value lookup, explicit remote profiling CLI, driver
  extras and lockfile, bounded fetches/timeouts, sanitized errors and shutdown cleanup.
- BigQuery performs a cost dry run before the real job and attempts cancellation
  on timeout; the extra operation is charged to the active shared DB-call budget.
- Scripted integration tests cover all four engines via `/api/chat`, source scope,
  forbidden SQL, parameter binding, cost rejection, cancellation and unconfirmed roles.
- Full local verification: 477 passed, 1 skipped Python; 127 frontend passed;
  Ruff, mypy (59 source files) and diff checks passed. These are not live-server tests.
- Official driver import smoke tests passed for MySQL, Snowflake and BigQuery.
  Snowflake import failed under SQLAlchemy 2.1.6; pinning below 2.1 resolved it.
  SQL Server's Python driver is installed but its system ODBC library/driver is absent.
- Live DB credentials/IAM were not provided: live remote queries, live-provider SQL
  accuracy and production performance are NOT RUN. Role confirmation is an admin
  acknowledgement, not automatic server-permission verification. See EXTERNAL_DATABASES.md.
- Existing `.env` credentials and unrelated image additions/deletions were preserved.

## Historical inspection and categorical evidence hardening — local follow-up

- Added `RunHistoryStore.inspect_result` and POST `/api/results/inspect`, reusing
  history and accepted state. Both run/result IDs resolve; returned rows are bounded
  retained snapshots, never silently re-executed or installed as active results.
- Incomplete previews report `complete=false`, `calculation_allowed=false` and a
  warning. Tests cover 19, 20, 21 and 40 rows, memory/disk stores, database/session
  isolation, failed results and resets. Inspection reset cutoffs persist in the
  existing history SQLite database; audit records remain available separately.
- Existing bounded live category lookup now supports parameterized exact matching,
  complete-domain/ambiguity/provenance metadata, remaining cache lifetime, and
  profile-version invalidation. No automatic fuzzy substitution or new scans.
- Removed fallback bottom slider, retaining inside zoom. Explicit comparison-period
  KPI cards may show verified changes/sparklines; no inferred comparison bubble is
  displayed. Existing icons, shadows, raw rows and SQL are preserved.
- Isolated 1,000-call in-memory history microbenchmark: p50 0.0088 ms, p95
  0.0091 ms, zero model/business DB calls. No equivalent baseline or live-provider
  performance comparison was measured; these numbers are not production latency.
- Remaining: Top-layer historical tool exposure, broader fuzzy/alias category
  indexing, end-to-end semantic comparison enforcement, authenticated multi-user
  authorization, PostgreSQL/live-provider checks and browser visual regression.

Shared `chart_theme.js` and CSS palette tokens now align Plotly and ECharts.
Updated line/step markers, tooltip surfaces, legends, bar spacing, pie spacing,
treemap boundaries, signed heatmap scales and box styling without changing source rows.
Added renderer tests for cross-renderer series-color stability, reduced motion,
step geometry and missing-value preservation. Browser screenshot comparison is
NOT RUN: no browser executable or connected browser is available locally.
No backend generation, KPI calculations, SQL execution or provider calls changed.
# SQL-based historical memory refinement — 2026-10-30

Implemented indexed accepted-SQL memory, explicit historical-source delegation,
validated current-data refresh, snapshot-only historical figures, bounded chart
preview retention and transactional refresh commits. See `docs/SQL_MEMORY.md`
and `tests/test_sql_memory.py` for contracts, measurements and acceptance tests.
Live PostgreSQL/provider verification is NOT RUN. No commit or push requested.
Verification: Python 487 passed, 1 skipped (12.78s); frontend 127 passed;
Ruff passed; mypy passed across 59 source files; `git diff --check` passed.
Ten new SQL-memory acceptance cases pass. Existing dependency deprecation
warnings remain. Local metadata benchmark: p50 1.602 → 0.090 ms; p95
1.734 → 0.098 ms, without model or business-database calls.

## Conversational visualization integration — 2026-10-30

Retained one Top-layer orchestrator and existing LLMChartProposals planner.
Added accepted grain/completeness/result identity to chart context, validated
model-free chart-type edits, missing-provider support for those edits, and a
failed-historical-refresh stale-source guard. Historical refresh → chart now has
an actual SQLite integration test. See `VISUALIZATION_INTEGRATION.md` for
measured call-count changes and the explicitly simulated latency benchmark.
Verification: Python 489 passed/1 skipped (12.61s); frontend 127 passed (264ms);
Ruff, mypy (59 source files), and diff checks passed. Added failed historical
refresh → visualization regression: no stale chart, no replacement SQL query.
Live provider, PostgreSQL and browser screenshot verification remain NOT RUN.

## Premium chart presentation refinement — 2026-10-30

Preserved prior uncommitted orchestration work. Frontend-only changes:
- Shared metadata-aware number/period formatting across charts, KPIs and tables;
  explicit compact/exact modes, unchanged raw values, validated calendar dates.
- Default eight-color palette tested at >=3:1 contrast against white for graphical
  marks; collision-aware series assignment retains colors within a palette.
- 3px unsmoothed lines, 8px circular markers with 2px white borders; existing
  disabled bottom slider and zoom/pan/reset behavior preserved.
- Treemap tile separation, exact scatter hover labels, comparison-only visible
  KPI sparklines, focus states, dark surfaces and mobile wrapping.
- Cache-busted frontend assets and 22 new tests spanning all 19 chart types,
  raw-data integrity, formatting, contrast and responsive sizing constraints.
Verification: frontend 149 passed; Python 489 passed/1 skipped; Ruff/mypy/diff
checks passed. Existing tests cover renderer rejection fallback, comparison
evidence and no-network display changes. No live browser pixel-level or screen
reader verification performed. Color contrast does not prove color-vision
deficiency distinguishability. No SQL/LLM calls added; no runtime speedup claimed.

## Integrated first-layer visualization proposals — 2026-10-30

Added optional existing LLMChartProposal to AgentDecision and strict native JSON
schema/examples. Request-local proposal propagation validates after governed SQL;
valid proposals skip the plotting LLM, missing/rejected proposals use the same
existing recommender. Shared validation rejects silent measure/series changes.
Top-layer chart-only reconciliation preserves completion flags. Prior uncommitted
frontend and memory work remains intact. See VISUALIZATION_INTEGRATION.md for
measured calls and the scripted latency benchmark (not production claims).
Acceptance tests cover two-call SQL+chart, fallback, historical refresh, chart
reuse/lineage, malformed decision repair, incomplete results, incompatible units,
duplicate grain and exact multi-measure preservation. Live-provider/PostgreSQL
verification remains NOT RUN; no commit/push requested.
Verification: 507 Python tests passed, 1 skipped (13.93s); 149 frontend tests
passed (317ms); Ruff, mypy across 59 source files, and diff checks passed.

## Compact chart polish — 2026-10-30

Kept existing chart heights and KPI calculations. Added shared readable titles,
non-mutating category label thinning/ellipsis, width-aware tick budgets, legends
above Plotly charts, stable pie/donut source order, cleaner composition labels,
readable KPI titles and accessible renderer-failure messages. Exact source labels
remain in hover/data; no SQL, model, result identity or analytical behavior changes.
Asset versions updated so browsers fetch the current presentation. New frontend
cases assert 21-row integrity while thinning labels, unchanged 260px sample height,
original pie values/order, title semantics and dense-line behavior. No browser
screenshot/visual QA performed; these are renderer-option and DOM-mocked checks.
Verification: 153 frontend tests passed (304ms); Python 507 passed/1 skipped
(15.53s); Ruff, mypy across 59 source files, and diff checks passed.

## Stable chart selector and history restoration — 2026-10-30

- Chart choices preserve the original recommendation order while selected types
  change; compatibility derives from accepted encodings, not the current type.
- Browser display snapshots retain only complete chart rows/specifications with
  session/database/run/result/chart identity and SQL/column checks (100-view cap).
  New conversation resets analytical state without deleting displayed history;
  Clear history clears the browser cache.
- The history-turn endpoint hydrates exact complete charts from a bounded
  process-local index of existing RecentResult snapshots, keyed by session,
  database and run, validating result/chart IDs and SQL. It does not overwrite
  active state or execute SQL. This supports page reload while the server retains
  the snapshot. Clearing history removes its display index.
- Persisted history stays bounded to its configured preview. After cache eviction
  or server restart, incomplete previews remain incomplete; no hidden rerun or
  fabricated chart. UI metadata distinguishes complete retention from previews.
- Tests cover stable type switching, 19/20/21/100-row browser restoration,
  identity/session/database isolation, invalid snapshots, cache bounds and a
  real SQLite 21-row chart across new conversations/page reload. The API test
  forbids business DB execution while opening history and checks restart behavior.
Browser visual testing remains NOT RUN. Existing uncommitted work is preserved.
Verification: frontend 160 passed; Python 508 passed/1 skipped (16.38s);
Ruff, mypy and diff checks passed. Initial full run had an existing optimizer
timing-dependent assertion fail; isolated rerun and full rerun passed unchanged.
Already-discarded old snapshots cannot be reconstructed without an explicit rerun.

## Deterministic dashboard Phase 1 — 2026-10-31

Implemented focused typed dashboard service/models, scoped cache-only API, and
separate dashboard_view.js module (three-line app integration). Fixed sections
are filters → KPIs → charts → optional table. No agent/LLM, database or schema
query capability; existing chart validation/rendering is reused. KPI eligibility
uses accepted SQL AST, disjoint output groups and retained source-grain metadata,
not business-name heuristics. Distinct counts, averages of averages, joins and
insufficient comparison evidence are rejected for reaggregation. See
`docs/DASHBOARD_MVP.md` for contracts, benchmark evidence and limitations.
The unrelated working-tree deletion of improvement.md was not modified.
Verification: Python 528 passed/1 skipped; frontend 170 passed; Ruff, mypy
(60 source files) and diff checks passed. Local 30-build benchmarks: 4-row p50
1.139ms/p95 1.398ms, 3,823 JSON bytes; 500-row p50 18.384ms/p95 19.012ms,
101,027 bytes. These measure backend build cost, not browser/network latency.
Additional SQL/LLM calls are zero, tested with execution/inspection/generation
forbidden after initial setup. Browser pixel-level mobile/desktop, PostgreSQL
and live-provider verification remain NOT RUN. No commit or push requested.

## CTE authorization and PostgreSQL timeout hardening — 2026-10-31

Replaced global CTE-name exclusion with lexical scope resolution on a normalized
AST copy, preserving actual SQL identifiers. Remote preparation independently
checks each physical table against its allowlist in addition to existing schema
and catalog checks. Fanout analysis uses the same physical-source classification.
Added adversarial tests for qualified CTE shadowing, nested/forward/self scopes,
quoted alias case, qualified identities and permissive-validator defense in depth
across four scripted adapters; positive CTE cases remain supported.

Replaced bound SET LOCAL with parameterized transaction-local set_config and
tested clamping plus query binding. Real PostgreSQL 15.19/Psycopg 3 verification
passed against an isolated UTF-8 database and unprivileged role: timeout settings,
read-only mode, cancellation, recovery and no session-setting leakage. Server
stopped afterward. Full suite: 556 passed/1 skipped; final PostgreSQL focused
run: 8 passed/1 skipped; frontend: 170 passed; Ruff/mypy/diff checks passed.
See `docs/SECURITY_REGRESSIONS.md`. No production DB, commit or push was changed;
the unrelated improvement.md deletion remains untouched.

## Phase 2 AI dashboard designer — 2026-10-31

Added a one-call dashboard specialist under the existing Top-layer LangGraph
assistant. Typed proposals select eligible KPI IDs and validated chart candidates
from bounded result profiles, with deterministic materialization, lineage checks,
unchanged data, fixed section order and safe fallback. Revisions use patches to
preserve unaffected widgets; scoped RAM revision history supports undo and
compare-and-set commits. Missing analytical data returns REQUERY_REQUIRED to the
Top layer; an accepted governed requery resumes the original dashboard goal.

The explicit UI now offers deterministic/AI design, inline design-change requests,
automatic validated previews and undo. Existing Plotly/ECharts/table callbacks
remain the only renderers. Config is documented in .env.example; no local .env
was edited. Source modules: ai_dashboard_designer, dashboard_service, runtime,
conversational_agent, api, models and task budgets in llm; frontend dashboard_view,
small app integration, styles and cache-busted index. See INTELLIGENT_DASHBOARD_DESIGN.md.

Acceptance coverage: 15 designer tests plus existing Phase 1 tests; frontend
dashboard coverage expanded to 12 tests. Full Python run: 570 passed/2 skipped;
frontend: 172 passed. Ruff, mypy (61 modules) and git diff --check passed.
30 identical four-row scripted builds measured Phase 1 p50/p95 1.266/1.662 ms
and Phase 2 2.868/4.173 ms. These exclude live inference/network/browser time.
Top design acceptance uses one Top call + one specialist call, no final-answer
call or SQL; initial invalid designs do not retry. API integration forbids DB
execution/inspection/generation after its original real SQLite query.

Limitations: live-provider/PostgreSQL checks and pixel-level desktop/mobile QA
NOT RUN for this phase; no new multi-user authentication, durable dashboard
storage or editable global filters. Conservative annotation validation can reject
legitimate numbered titles/aliases. Existing security fixes and unrelated
improvement.md deletion were preserved. No commit or push was requested.

## Phase 3 Permanent Dashboard Studio — 2026-10-31

Verified the clean local main checkpoint (32e3426) and the existing Phase 1/2
services/tests before extending them. Added dashboard_store.py with typed retained
snapshots/documents and two additive migrations in the existing history SQLite
file. Durable revisions retain source SQL, rows, semantic definitions, result/run
identity, provenance, owner and timestamps. Evidence is revalidated before saving,
reopening and approving, without executing source SQL. Snapshot tables cannot be
exposed as a registered business database. Private file mode and serialized
field selection protect credentials/configuration; this is not universal DLP or
encrypted storage.

dashboard_studio.py provides scoped Library/open/Save/Save as New/rename/delete,
Draft/explicit owner-approved Published, history and append-only Undo. Atomic
SQLite version checks reject stale writes and copies. Drafts are hidden from
viewers; editors cannot approve/delete. Validated preview handles bind owner,
dashboard and base version; consecutive unsaved edits preserve the previous
preview. A model-time version change rejects the stale preview. Approval validates
before committing, so corrupt/unsupported evidence cannot partially publish.

The existing LangGraph Top layer and Phase 2 designer handle saved design edits
through isolated request-local branch context. Saved editing forbids SQL/metadata
DB tools, even on an adversarial Top decision. It cannot refresh or overwrite the
active conversation. New analytical data requires a separate governed analysis.
Frontend dashboard_studio.js reuses the existing dashboard/Plotly/ECharts/table
renderers and adds a Library dialog, explicit save/approval/version controls and
unsaved previews. Only one-line integration hooks were added to app initialization
and dashboard mounting. No new agent/framework/chart types/global filters.

Checks: full Python 593 passed/2 skipped (18.02s); frontend/voice 180 passed;
Ruff, mypy (63 source modules), and git diff --check passed. New acceptance cases:
14 storage cases, 9 API/orchestration cases, 4 frontend cases. Valid scripted
saved editing uses one Top call plus one designer call; reopening uses zero model
and zero business-DB calls. Tests cover 0/1/21/100/500 complete rows, restart,
concurrency, ownership, ACLs, approval, invalid evidence, undo, history isolation,
configuration-secret exclusion and source consistency. These counts are not a
live-provider latency or accuracy benchmark.

External sharing remains DISABLED/BLOCKED: no legitimate deployment login/session
ownership mechanism exists. Local Studio is loopback-only, server-owned workspace
access, not production authentication. Trusted ASGI stable identities and explicit
authorized-session scope are required for remote creation. No fake login, header
identity, automatic session claiming, or public/grant API was added. Exact missing
deployment prerequisites are in DASHBOARD_STUDIO.md. Live providers, PostgreSQL and
real desktop/mobile browser visual QA NOT RUN. RAM previews need sticky routing
across workers and are not durable until Save; published snapshots remain permanent.
No commit, push, actual .env edit, new DB refresh or external infrastructure change.

### Phase 4 conversation-first multi-result dashboards (2026-10-31)

Implemented automatic dashboard composition through the existing Top-layer agent
and bounded designer. Independent governed SQL objectives retain separate typed
source identities, contracts, snapshots and provenance; no cross-result joins or
aggregations are introduced. Ordinary SQL questions expose no dashboard-creation
controls. Selected-component edits preserve unaffected widgets, and explicit
analytical additions append accepted evidence without replacing old snapshots.
Saved v1 dashboards remain readable; v2 documents reuse the existing store/CAS.

Designer inputs omit raw rows and category values. One targeted correction is
allowed for repairable invalid proposals under the shared budget. The example
model-call ceiling is 12; the actual local .env was not changed. Dashboard scope,
KPIs, charts and optional anchor table retain the fixed section ordering.

Verification: full Python 605 passed/2 skipped (17.92s); frontend/voice 180 passed
(537ms); Ruff, mypy (64 source modules), and git diff --check passed. New evidence
is in test_multi_dashboard.py (11 cases), extended test_dashboard_studio.py,
dashboard_view.cjs and dashboard_studio.cjs. Real SQLite with scripted providers
verifies three SQL objectives followed by composition: 4 Top decisions + 3 SQL
generation calls + 1 designer call, with no separate final-prose call. A saved
analytical addition uses 2 Top + 1 SQL generation + 1 designer call. These are
fixture measurements, not production latency/token or semantic-accuracy claims.

Limits: four sources in one authorized database, four KPIs/four charts, complete
bounded results only, optional data table for the anchor source. No interactive
global filters or automatic refresh. Temporary bundles require sticky routing;
saved snapshots survive restart. External sharing stays BLOCKED on legitimate
deployment identity/session authorization. Live providers, PostgreSQL and browser
visual QA NOT RUN. No commit or push; restart/reload is needed to load the UI.
See AGENTIC_DASHBOARDS.md for source files, behavior and remaining boundaries.

Publication review: rejected duplicate effective replacement targets before
materialization, preventing silent same-widget overwrite. Regression preserves
the original widgets on rejection. Final checks: 606 Python passed/2 skipped
(18.12s), 180 frontend/voice passed (515ms), Ruff/mypy/diff passed. Live-provider,
PostgreSQL and browser visual checks remain NOT RUN.

### Human-in-the-loop dashboard design (2026-10-31)

The same LangGraph assistant now investigates and proposes before rendering a
dashboard. Typed DashboardDesignProposal retains per-widget goals/reasons, KPIs,
chart/source requirements, fixed section layout, bounded statistical observations
and a versioned discussion. The existing designer and governed SQL pipeline are
reused; no new autonomous agent, SQL executor or NL intent router was introduced.

Additive migration 3 in the existing private Studio/history database stores
pending discussions and revision history. Candidate preparation stages are
committed atomically; exact-version approval, BUILDING/READY/FAILED transitions,
rejection and expiry preserve evidence. Invalid revisions retain the prior
proposal. Failed durable finalization rolls back the unexposed RAM revision and
bundle. Existing owner/editor/viewer checks and saved-version checks remain.
Saved Studio reopening exposes a pending discussion to authorized editors/owners;
new conversations never imply consent. Reset/Clear is session-scoped. Source
snapshots are explicitly historical, not automatically refreshed live data.

Only explicit confirmation of the current version builds a complete design;
layout-only revisions and complete-proposal approval cause zero business-DB calls.
An approved plan with missing analyses continues through governed SQL, then asks
for review of newly verified mappings. Save and Publish remain independent.
Frontend pending proposals render as discussion, not Chart unavailable or empty
dashboard tabs. Existing Plotly/ECharts rendering and all result identities remain.

Final verification: 621 Python passed/2 skipped (19.75s), 182 frontend/voice passed
(554ms); Ruff, mypy (65 source files), and git diff --check passed. New acceptance
coverage: test_dashboard_proposals.py (15 cases), extended multi-dashboard/Studio
integration and frontend discussion/status tests. Real SQLite + scripted providers
measure: three-result proposal = 4 Top + 3 SQL generation + 1 designer; complete
approval = 1 Top + 0 SQL/designer; valid layout revision = 1 Top + 1 designer and
0 SQL; missing-evidence continuation = 3 Top + 1 governed SQL generation + 1
designer, then separate confirmation. Invalid revisions permit at most one
targeted designer correction. These are fixture call counts and test runtimes,
not live-provider latency, analytical accuracy or production speedup measurements.

Live providers/PostgreSQL/browser pixel QA NOT RUN. Production identity/session
authorization and external sharing remain BLOCKED as previously documented.
Unsaved discussions resume in their original session, not by implicit cross-chat
transfer. In-flight investigation uses existing progress/budgets; a cancelled
attempt does not replace the last committed proposal. Legacy explicit dashboard
APIs remain compatibility interfaces. Actual .env, credentials, business data and
unrelated files were not edited. No commit, push or server restart was performed.
This approval-based workflow is historical; see AUTONOMOUS_DASHBOARDS.md for
the current workflow and remaining boundaries.

### Dashboard decision-format hardening (2026-10-31)

Recent local history confirmed rejected decision and repair calls; the exact old
provider output/validation reasons were not retained, so their specific cause
cannot be reconstructed. Confirmed source defects: prompt examples omitted newly
required tool metadata, the prose key list omitted instruction, no dashboard
proposal/approval examples existed, and failures appeared as normal no-query
answers. Examples now expand through AgentDecision defaults and cover both
dashboard actions. Required references/approval checks and the one-repair limit
are unchanged. No invalid decision may execute a tool.

Failed call-ledger entries now retain bounded validation issue lists, not raw
provider responses. The frontend identifies assistant-service failures rather
than calling them answered requests. ValidationError is handled before its
ValueError parent, avoiding raw input/exception disclosure from unexpected
structured-state failures. Native-schema/example completeness, failed usage,
safe error text and frontend failure classification have regression assertions.
No provider configuration, credentials or SQL behavior changed. Live reproduction
of the original Groq response remains NOT RUN; restart/retry is needed to confirm
the user's request and obtain precise diagnostics if it still fails.
Checks: full Python 622 passed/2 skipped (20.49s), frontend/voice 182 passed
(464ms), Ruff, mypy (65 source files) and git diff --check passed. No commit,
push or server restart was performed.

### Provider throttling and request-budget diagnostics (2026-10-31)

The latest retained dashboard trace contained valid Top decisions, a successful
SQL execution, a premature designer request without evidence, and repeated Groq
transport retries before exhaustion. The provider adapter only performs these
transport retries on HTTP 429. The recorded request took 107,491ms; this is a
single observed failure, not a latency benchmark or success-rate estimate.

Kept the existing call ceiling. Groq now checks global/specialist retry capacity
and remaining time before waiting; it never retries earlier than a long provider
Retry-After just because the local wait ceiling is 30 seconds. If it cannot honor
the delay, it returns the typed 429 rather than making premature attempts. A denied
retry preserves the preceding HTTP failure and does not inflate retry counts.
Resource guards remain authoritative and all real sends still consume the budget.

The Top layer now stops without another model invocation when no capacity remains,
preserving the last tool failure instead of hiding rate limiting behind a generic
decision error. Prompt/example ordering emphasizes investigation first and forbids
designer use without accepted evidence or a pending proposal; the LLM still owns
intent selection. Deadline and model-call messages are distinguished. Regression
tests cover shared/specialist exhaustion, long Retry-After, insufficient deadline,
transient success and retaining a throttled SQL-tool failure without another Top
call. No quota, credentials, actual .env or live provider settings were changed.
Real-provider completion of the dashboard remains unverified; code cannot remove
the provider's external TPM limit.
Checks: Python 626 passed/2 skipped (20.34s), frontend/voice 182 passed (576ms),
Ruff, mypy (65 source files) and git diff --check passed. Provider failures used
mock HTTP transports; no live provider request, commit, push or restart was made.

### Working local dashboard/SQL integration (2026-10-31)

Confirmed blockers: designer inference was unconditionally capped at 12s; a live
Qwen JSON-mode request echoed the JSON schema rather than a proposal; the user's
SQL provider was now Ollama but ModelProvider/catalog/SQL routing did not admit
it. No Queryflow API listener was running, although Ollama was available.

The designer now uses a configurable, request-deadline-bounded timeout (local
default 120s, API default 12s). Native Ollama DashboardProposal output prevents
schema echoing; an explicit unsupported-grammar response can use the existing
single bounded correction/fallback. Native-mode prompts avoid duplicating the
schema and include a valid instance shape with real eligible references.

OllamaCompleter now implements generate using the existing SQL prompt and SQL
task token limit. RoutingSQLModel reuses the same local client used by reasoning;
no new executor or agent was added. Provider/model types and catalog registration
admit explicitly environment-configured Ollama model IDs, not arbitrary client
model names. Truncated local output fails rather than executing partial SQL.
Mock HTTP + real SQLite tests prove safe execution and rejected DELETE attempts;
native dashboard and bounded unsupported-grammar paths are covered.

Live local synthetic evidence: old JSON mode FALLBACK/schema echo after 96.12s;
new native mode READY, two charts, one call, 12.94s, zero SQL/cloud calls. Warmup
was not controlled: no speedup ratio or general accuracy claim. A real business
dashboard and live local SQL generation have NOT been tested end to end.

Final full suite: 632 Python passed/2 skipped (22.94s), 182 frontend/voice passed
(679ms), Ruff, mypy (65 source files), diff check passed. One earlier full run had
a timing-sensitive optimizer NOT_FASTER assertion fail; it passed alone and on
the final full run. No optimizer logic/test expectations were changed.

Started updated API locally at 127.0.0.1:8000, process 48675. HTTP configuration
checks confirmed SQL=qwen3.5:4b/Ollama, assistant=llama3.2:latest/Ollama, both models
registered, and debit_card_specializing available among 12 databases. Local .env
retains the selected designer=qwen3.5:4b/Ollama and adds designer timeout=120s;
other role settings were not changed. No Git commit/push. Deployment authentication,
external sharing, live cloud providers and browser visual QA remain unverified.

### Real dashboard API investigation — NOT COMPLETE (2026-10-31)

Executed four isolated, authorized `/api/chat` dashboard tests using the live
local models and read-only debit_card_specializing database. No valid dashboard
was built, approved, saved or published. Attempts took 27.78s, 57.93s, 55.73s and
38.38s respectively. Observed failures: investigation loop without a proposal;
SQL execution followed by failed design validation; an empty proposal backed by
zero rows (SQL invented a 202% date filter and spending joins); and accepted SQL
followed by repeatedly rejected designer actions before the step limit. HTTP 200
was not treated as dashboard success or business correctness. No manual SQL or
fabricated chart was substituted to make the test pass.

Confirmed telemetry defect fixed: investigation-limit exits dropped ledger/usage
and spent a ninth decision after eight tools. They now preserve usage/tool trace
and stop without that additional call. Designer and failed-tool validation reasons
are retained, invalid design output terminates rather than being misclassified as
missing data for another full designer retry, and empty supported designs cannot
be approved without widgets or an explicit pending-analysis requirement. These
are diagnostic/safety fixes, not a claim that the small models are now reliable.

Final checks: 634 Python passed/2 skipped (29.05s), 182 frontend/voice passed
(470ms), Ruff, mypy (65 source files) and diff check passed. These regression
results do not supersede the failed live API outcomes. Left the API running at
127.0.0.1:8000, process 50214, with role settings unchanged. Asked permission to
try Qwen for the understanding layer as well; no implicit model switch was made.
Next uncompleted work: choose an authorized understanding/model configuration,
obtain a meaningful nonempty supported proposal through the API, review and
confirm its exact version, then verify READY and lineage. No commit or push.

### Llama-preserving live dashboard completion (2026-11-01)

Kept user-selected Llama 3.2 understanding and Qwen 3.5 4B SQL/design unchanged.
Live compatibility probes isolated Ollama grammar rejection to large maxLength
constraints; removing only those above 1024 on the wire compiled successfully.
Canonical prompt schemas and local Pydantic limits remain enforced. Added native
ANSWER/CALL_TOOL agreement branches, contextual result/proposal enums and local
reference validation with the existing one-repair limit. No new NL router/agent.
Unavailable row-dependent tools are excluded before evidence exists. Preserved
original client constraints as bounded context in scoped SQL handoffs.

Successful real API proposal: 58.77s; Qwen SQL summed yearmonth.Consumption by
Date over all available dates, with no invented date predicate, joins or extra
metrics. Result: 21 complete rows, one supported line chart, no missing evidence.
First confirmation was correctly blocked because Llama paraphrased the approval
quote. Added a contextual exact-quote enum plus local pre-tool checking; after
restart the same proposal/version was confirmed and reached READY in 4.86s.
Successful approval used one Llama decision and zero SQL-generation, business-DB
execution or designer calls. Result ID and all 21 chart rows match the approved
source. No Save or Publish action, no cloud calls, no fabricated KPI was used.
One earlier automatic chart attempt rejected a spurious requested customer metric
before a model/DB call; the Top layer continued to the valid dashboard proposal.
That redundant attempted path remains a refinement opportunity, not hidden work.

New tests in test_scoped_decisions.py cover wire/canonical separation, immutable
scoped schema references, invalid-ID repair before tools, unchanged user scope,
action/tool grammar consistency and exact approval quoting. Full Python: 641
passed/2 skipped (24.16s); frontend/voice: 182 passed (519ms); Ruff, mypy (65 source
files) and diff checks passed. Synthetic greeting now validates natively under
Llama without grammar fallback. Passing this live explicit-schema consumption
scenario does not establish broad business-question accuracy for these models.

API remains at 127.0.0.1:8000, process 63593. Temporary dashboard is in an isolated
API test session, not the browser's active conversation or Dashboard Library.
Its supporting proposal remains durable; it was not saved/published. No .env
model changes, commit or push. Production auth/sharing and browser visual QA
remain outside this verification.

## 2026-11-01 — Compact progress and design-review presentation

Follow-up refinement: conversational scope prompt directs unrelated questions to
New conversation; dashboard investigation subgoals remain supported. Designer
requests three supported KPIs where eligible, never a fabricated minimum quota.
Full-width chart rows and shared KPI icons/sparklines/comparison rendering are
implemented. Backend observations require proved additive single-time grain.
642 Python passed/2 skipped, 187 frontend/voice passed, Ruff/mypy/diff passed.
No live provider or browser QA; backend prompt changes require process reload.

Baseline committed as `5ce519b` before presentation changes. Added five visible
business stages while preserving inspector timings and raw activity events.
Added a compact evidence-backed proposal card with explicit confirmation and a
composer-focused Request changes action. Restored/stale approval controls are
disabled. Waiting is informational, not a running build. No provider/model or
SQL execution changes. No push performed.

Checks: 641 Python passed, 2 skipped (22.05s); 186 frontend/voice passed (548ms);
Ruff and mypy (65 source files) passed. Browser visual QA and live-provider
rechecks were not run for this presentation-only change.
## 2026-11-01 — Autonomous dashboard design supersedes approval

Implemented the two supplied autonomous-dashboard specifications. Existing Top
LangGraph investigation, governed SQL, numerical tools, bounded designer repair,
typed candidate validation and permanent Studio are reused. Validated designs and
edits now render immediately. No active confirmation/version-approval dependency;
legacy proposal tables remain archival and do not seed sources or build authority.
Historical UI cards have no approval controls. Save/Undo/Versions/Publish remain
explicit. The designer prompt is the supplied senior BI/EDA/design prompt with
existing schema and application layout constraints attached.

Incomplete analytical requirements return to the Top layer for bounded governed
requery. Fallback/invalid designs are terminal structured failures, not completed
dashboards. Expected revision is captured before model awaits; a racing edit wins
without being overwritten. Ready dashboards bypass ordinary chart-unavailable UI.

Evidence: 643 Python passed, 2 skipped (21.50s); 186 frontend/voice passed (515ms);
Ruff, mypy (65 source files), diff checks passed. Tests cover automatic multi-source
build/save/reopen, saved layout edits with zero SQL, requery/build without approval,
legacy immutable pending records, failed edits and concurrent revision conflicts.
Replaced obsolete live-approval integration expectations with archival safeguards;
legacy store transition tests remain. See AUTONOMOUS_DASHBOARDS.md for scripted
before/after call counts. No live provider/PostgreSQL/browser pixel QA was run,
no model/environment changes, no commit/push or backend restart performed.
