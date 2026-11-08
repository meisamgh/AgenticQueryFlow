# Queryflow V6.4 requirement matrix

2026-11-08 explorer redesign: authority transitions, mixed lineage, configured
local-source access, KPI default selection and deterministic layered positioning
have passing regression evidence (950 Python / 221 frontend tests in the full
run). Production datasource identity remains BLOCKED. Exact three-viewport,
light/dark browser acceptance remains NOT VERIFIED, including final layered
layout screenshots; see SEMANTIC_EXPLORER.md. No general semantic correctness
or production authorization requirement is promoted by these explorer checks.

Explorer extension (2026-11-08): local bounded schema/KPI graph APIs and frontend
regressions VERIFIED; desktop schema/KPI rendering checked. Production datasource
authorization BLOCKED; mobile/dark browser QA NOT RUN. This does not promote any
previously unresolved V6.4 semantic or production requirements. Evidence and
limits: [SEMANTIC_EXPLORER.md](SEMANTIC_EXPLORER.md).

2026-11-08 user default override: graph grounding/persistence, KPI grounding,
KPI-first retrieval and local ontology recovery default true. Explicit opt-outs
remain available. test_semantic_feature_defaults.py and startup-persistence
regressions verify the defaults; historical default-off notes below are superseded
only for these features, not for authorization, live integrations or untrained ML.

2026-11-08 filter position audit: CASE/CAST/boolean wrappers cannot certify an
enforced predicate; only required positive WHERE conjunctions can establish proof.
Eight new regressions preserve real conjunct proof and explicit uncertainty for
unsupported wrappers. General boolean and cross-scope equivalence remain open.

2026-11-07 final output audit: explicit qualified physical outputs are checked
against final scoped lineage, not just retrieved/filter/grouping columns. New
tests distinguish provable omission, valid renamed/CTE projection and unsupported
transformation/wildcard/scope proof, and verify execution is blocked before repair.
General output expression equivalence and business correctness remain unproven.

2026-11-07 KPI output/root audit: explicit physical outputs are pinned and invalid
identifiers fail before generation; combined roots require shared dimension
support. Generic follow-ups preserve requested roots with revalidated accepted
provenance rather than projecting the whole dependency closure. Three regressions
in test_kpi_first.py support these local claims, not overall semantic completion.

2026-11-07 KPI filter audit: live user filter/time operands enter mandatory schema
requirements before graph planning; removed filter/period dependencies invalidate
derived joins while preserving active constraints. New real SQLite and typed
delta regressions are in test_kpi_first.py; the whole ContractDelta matrix and
general semantic correctness remain IN_PROGRESS.

2026-11-07 KPI delta audit: conversation.py invalidates superseded derived
grounding while preserving unaffected metric root identity and active column
requirements. Five additional KPI follow-up regressions cover metric switching,
dimension removal, filter retention, real SQLite execution and closure continuity.
This is not a claim that the full ContractDelta or semantic matrix is complete.

2026-11-07 KPI-first opt-in: implementation and phase evidence are recorded at
the top of V6_4_PROGRESS.md and in SEMANTIC_GRAPH.md. test_kpi_first.py covers
36 local acceptance cases. The new flag is false by default; complex formula,
time-grain and fanout proofs remain explicitly UNRESOLVED. No trained v2 model,
live-provider accuracy or production identity gate is promoted by this task.

2026-11-07 SQLite failure handling: corrupt schemas, connection initialization
and stat failures use structured DatabaseError codes; initialization closes its
handle and inspection failures evict stale cache entries. test_sqlite_failures.py
verifies direct errors, API JSON and zero model calls for unusable schemas.
This does not promote live-provider or production-identity gates below.

2026-11-07 SQLite freshness: read-only connections respect committed WAL data;
the existing stat cache includes WAL identity, and schema inspection uses one
read transaction. test_sqlite_wal.py verifies visible committed/uncommitted
boundaries, WAL-only invalidation, cache reuse and concurrent inspection. No
live remote/provider or general semantic-correctness gate is promoted.

2026-11-07 SQLite file identity: connection URIs encode the resolved filesystem
path before read-only options. test_sqlite_uri_paths.py verifies special-character
roots, schema inspection, exact source identity and read-only writes rejected
even after query_only is disabled. This is a local SQLite safeguard, not evidence
of remote-engine/production authorization completeness.

2026-11-07 result-boundary hardening: non-finite floats fail structurally before
JSON serialization across adapters; finite values and NULL remain unchanged.
Real synthetic SQLite, serializer, API failure, failed-acceptance and bounded
repair tests are in test_nonfinite_results.py and test_agent.py. This does not
prove general numerical/business semantic correctness or live engine behavior.

2026-11-07 metadata-write hardening: ProfileStore uses contained direct-child
destinations, refuses file symlinks, atomically replaces complete JSON and evicts
saved cache entries to avoid concurrent-writer timestamp misassociation.
test_profile_save_boundaries.py covers partial-write/replacement failure,
symlink escape, path traversal and the cache race. These local safeguards do not
promote the broader semantic or production-identity requirements below.

2026-11-07 audit continuation: embedded profile dialect and graph/profile dialect
consistency are now checked independently (test_profile_dialect_boundary.py).
Configured Ruff and mypy failures from the merged sidecar/reset helpers were
repaired without suppressions. NetworkX is default-on at the user's request;
historical Phase 0 stop/default-off notes do not supersede that request. No
general semantic, production identity or live-provider requirement is promoted.

2026-11-04 amendment: Phase 0 evidence is in SEMANTIC_GRAPH_PHASE0.md and
benchmarks/results/semantic_graph_phase0.json. Runtime changes are STOPPED at the
review gate. Typed ambiguity warnings, independent persistence gating, and later
KPI/ontology refinements remain PENDING; no older requirement is promoted.

Graph-enhanced grounding (2026-11-04) is tracked separately in SEMANTIC_GRAPH.md
and test_semantic_graph.py. The optional layer does not promote any older
IN_PROGRESS/BLOCKED semantic, authorization or live-provider requirement below.

Statuses: NOT_STARTED, IN_PROGRESS, IMPLEMENTED_UNVERIFIED, VERIFIED, BLOCKED, NOT_APPLICABLE. VERIFIED requires code evidence and a relevant passing test. This matrix is a checkpoint, not a completion claim.

| ID | Requirement | Severity | Current status | Source files | Planned change | Test coverage | Acceptance condition | Evidence | Final status |
|---|---|---|---|---|---|---|---|---|---|
| V64-01 | Correct grouping expressions | P0 | IN_PROGRESS | `validator.py`, `agent.py` | Match actual group keys and aliases | `test_semantic_validation.py` | Wrong dimension rejected, equivalent form not falsely rejected | Direct wrong dimension, alias, ordinal and CTE tests pass; transformed lineage remains unresolved | IN_PROGRESS |
| V64-02 | Correct aggregation and measure lineage | P0 | NOT_STARTED | `validator.py`, `semantic_catalog.py` | Trace aggregate input to required measure | `test_semantic_validation.py` | Wrong measure rejected | Direct physical input and simple approved SUM formula tests reject wrong columns; casts, competing aggregates and derived formulas remain unresolved in passing tests | IN_PROGRESS |
| V64-03 | Predicate/filter correctness | P0 | IN_PROGRESS | `validator.py` | Match operand/operator/value in actual predicates | `test_semantic_validation.py` | Literal in unrelated expression not accepted | Wrong column/value and qualified-table tests pass; complex predicate equivalence remains | IN_PROGRESS |
| V64-04 | Temporal correctness | P0 | NOT_STARTED | `validator.py`, `conversation.py` | Reconcile active bounds and storage format | Date-range tests | Wrong period rejected when provable | Explicit temporal boundaries now enter predicate validation; direct wrong bound fails and transformed CTE remains unresolved in passing tests. Active-period equivalence remains open | IN_PROGRESS |
| V64-05 | Join fanout/cardinality | P0 | NOT_STARTED | `validator.py`, `context.py` | Use verified relationships and grain | `test_semantic_validation.py` | Provable fanout rejected | Many-to-many and explicit one-to-many `fanout_risk` aggregate joins yield UNRESOLVED; full grain proof remains | IN_PROGRESS |
| V64-06 | DISTINCT, ranking, NULL, units | P0 | NOT_STARTED | `validator.py`, `models.py` | Typed provable checks with UNRESOLVED fallback | Semantic negative tests | No false PASS | Final ORDER BY metric, direction and LIMIT are checked; wrong direct ranked metric fails, while complex lineage remains UNRESOLVED. DISTINCT, NULL and units remain open | IN_PROGRESS |
| V64-07 | Complete ContractDelta operations | P0 | NOT_STARTED | `conversation.py`, `models.py` | Reconcile dependent fields and clarify conflicts | `test_conversation.py` | Unaffected constraints retained | Existing preservation and conflict tests pass; stale CHANGE_PERIOD previous values now fail instead of silently changing the accepted period. Full delta set remains | IN_PROGRESS |
| V64-08 | Typed TurnContext and result identities | P1 | NOT_STARTED | `conversational_agent.py`, `models.py`, `result_cache.py` | Request-local state and stable IDs | First-turn SQL/plot identity tests | Chart source links exact result | Frozen TurnContext, result/chart IDs, same-turn NEW_QUERY → REFINE → chart, and result-tool read-your-writes tests pass; comprehensive identity matrix remains | IN_PROGRESS |
| V64-09 | Explicit state commit boundaries | P1 | NOT_STARTED | `api.py`, `conversation.py` | Remove intermediate persistence dependency | First-turn SQL+plot tests | No hidden state write | Request-private provisional state feeds subsequent SQL calls without intermediate persistence; original commit version is retained. Concurrent-commit, branch-replay, and cancelled-job preservation tests pass; full cancellation matrix remains | IN_PROGRESS |
| V64-10 | SQL/plot/requery/investigation tool sequences | P1 | NOT_STARTED | `conversational_agent.py`, `plotting_service.py` | Pending goals and bounded continuation | Integration scenarios 1–2, 12–15 | Goal completes or terminal status | Same-turn SQL then plot and saved aggregate → REQUERY_REQUIRED → governed SQL → validated chart pass end-to-end. New adversarial V6.3 tests cover wrong-model chart routing, recap reuse, typed filter correction, premature chart answer, and bounded requery; investigation and complete pending-goal matrix remain | IN_PROGRESS |
| V64-11 | History restore, reset, cancellation | P1 | NOT_STARTED | `api.py`, `run_history.py` | Restore exact source, enforce cancellation | History/concurrency tests | No stale result | Explicit restore still rebinds selected result; parent-run requests use a request-local snapshot/result and an unresolved branch leaves accepted state unchanged. Cancellation and full restore matrix remain | IN_PROGRESS |
| V64-12 | Remove competing routers | P1 | NOT_STARTED | `conversational_bridge.py`, `conversation.py`, `api.py` | Isolate compatibility routing | Routing tests | Top layer sole intent owner | `/api/chat` dispatches to the Top layer before consulting the legacy bridge flag; a both-flags integration test proves only the Top layer responds. Legacy bridge remains an explicit compatibility path | VERIFIED |
| V64-13 | VerifiedSchemaSnapshot reuse | P1 | NOT_STARTED | `service.py`, `agent.py`, `context.py` | Reuse request-scoped schema/profile | `test_agent.py` | Fewer redundant inspections, same authorization | Typed snapshot passed from service; direct agent test proves no second inspect | VERIFIED |
| V64-14 | Approved metric discovery | P1 | NOT_STARTED | `semantic_catalog.py`, `conversational_bridge.py` | One catalog-backed source | Discovery consistency test | Same approved definitions across tools | Conversation metric discovery now reads the configured semantic catalog and excludes inferred metrics; approval test passes. Glossary definitions still coexist, so complete unification remains | IN_PROGRESS |
| V64-15 | One PlottingService and sufficiency gate | P1 | NOT_STARTED | `plotting_service.py`, `api.py`, `visualization.py` | Consolidate chart ownership | Fresh-result and fallback tests | No insufficient chart accepted | PlottingService checks fresh and saved result sufficiency. PR #3 chart-type follow-up behavior is implemented locally: prior axes/metric/series are sent to the model, incompatible proposals are rejected, a validated fallback preserves encodings, and analytical period changes require SQL. Focused tests pass; full chart ownership consolidation remains | IN_PROGRESS |
| V64-16 | Task-specific provider limits and failures | P1 | NOT_STARTED | `llm.py`, `runtime.py`, `conversational_agent.py` | Separate output budgets and typed errors | Provider adapter and Qwen fallback tests | Truncation never silently accepted | Live synthetic Qwen full prompt reproduced strict `json_validate_failed`; JSON-object mode succeeded and locally validated. Groq budgets, safe typed errors, truncation and bounded fallback tests pass. Other adapters remain | IN_PROGRESS |
| V64-17 | Accurate budgets and telemetry | P1 | NOT_STARTED | `runtime.py`, `conversational_agent.py` | Count each actual model/DB call | Ledger tests | Counts match calls | Qwen native-failure + fallback test proves three calls and one failed ledger entry; Groq internal 429 retry now claims shared budget, and a test proves it cannot exceed the one-call cap. Full telemetry remains incomplete | IN_PROGRESS |
| V64-18 | Plotly async fallback | P2 | NOT_STARTED | `web/plotly_view.js`, `web/app.js` | Catch async render rejection | `frontend_rendering.cjs` | ECharts fallback after rejected promise | Rejected Plotly `newPlot` promise activates ECharts through the same stable chart handle in DOM-mocked test | VERIFIED |
| V64-19 | Frontend result synchronization | P1 | NOT_STARTED | `web/app.js`, `chart_explorer.js` | Bind SQL/Data/Answer/chart to result ID | Browser state tests | No stale chart | Chart rendering now rejects a mismatched source-result ID and records result/chart IDs on the response node; positive, mismatch and legacy cases pass. Full multi-turn browser identity matrix remains | IN_PROGRESS |
| V64-20 | Production session authorization | P1 security | BLOCKED | `api.py`, `run_history.py` | Integrate real identity when provided | Multi-user integration test | Cross-user isolation proven | No deployment identity mechanism configured | BLOCKED |
| V64-21 | Full SQLite end-to-end regression | P1 | NOT_STARTED | `tests/` | Real execution and multi-turn assertions | 32 required scenarios | State, SQL, chart and provenance verified | Existing tests plus accepted-contract, ranked-metric, stale-period and parent-branch regressions pass; the 32-scenario matrix is still incomplete | IN_PROGRESS |
| V64-22 | PostgreSQL/live-provider verification | P1 | BLOCKED | `tests/`, provider config | Run optional integration when reachable | Live integration test | Actual provider/DB result | Live synthetic SQLite books run succeeded through Qwen Top decision, JSON fallback, Model 1, SQL generation and read-only execution (fiction=2/history=1). Real debit-card egress was blocked by review; PostgreSQL was NOT RUN | BLOCKED |
# Historical SQL memory refinement

Acceptance coverage is in `tests/test_sql_memory.py`: indexed ordinal/ID lookup,
session/database/reset/branch isolation, real SQLite refresh, historical-source
modification, schema changes, unsafe SQL rejection, concurrent commits and
bounded snapshot retention. Verification results are recorded in the delivery
report; live provider and PostgreSQL checks remain NOT RUN.
SQL-memory acceptance cases: VERIFIED locally (10 cases); full Python suite
487 passed/1 skipped; frontend 127 passed; Ruff, mypy and diff checks passed.

Visualization refinement: existing SQL+plot+type-edit integration now asserts
one SQL generation and one plotting-model call across both turns (previously two
plotting calls). `test_historical_refresh_and_plot_share_new_result_without_regeneration`
verifies refreshed SQL/result/chart lineage. Broader V6.4 statuses above are unchanged.

Integrated proposal acceptance coverage: `test_integrated_chart_planning.py` and
the integrated/malformed/chart-only cases in `test_v63_conversation.py` validate
proposal schema, safe fallback, call counts and source identities. The historical
refresh integration runs with and without an embedded proposal. No claim that
all grain/unit/business semantics are provable; existing unresolved states remain.

### Phase 2 dashboard specialist (2026-10-31)

VERIFIED locally: typed eligible-catalog selection; exact result lineage; one-call
specialist budget; deterministic fallback; no-SQL design/undo; patch preservation
and revision CAS; Top → missing-data → governed SQL → resumed design; fixed
section ordering and existing renderer reuse. Evidence: 15 tests in
test_ai_dashboard_designer.py, existing test_dashboard.py, and 12 frontend cases
in dashboard_view.cjs. Full checks are recorded in V6_4_PROGRESS.md.
NOT RUN: live provider, PostgreSQL and pixel-level browser verification for this
feature. This does not change older V6.4 requirement statuses or claim production
tenant authentication/durable multi-worker dashboard state.

### Phase 3 permanent dashboards (2026-10-31)

VERIFIED locally: additive migration and restart recovery; typed complete source
snapshots; scoped library CRUD; Draft/explicit approval; append-only saved versions
and Undo; optimistic conflicts across independent SQLite connections; reference-only
Save and Copy; unchanged snapshot/source evidence; existing Top-layer/designer
integration; provisional preview continuity; blocked SQL refresh; renderer reuse.
Evidence: test_dashboard_store.py (14 cases), test_dashboard_studio.py (9 cases),
dashboard_studio.cjs (4 cases), full regression 593 Python passed/2 skipped,
180 frontend/voice passed, Ruff/mypy/diff checks passed.

BLOCKED for external sharing: real deployment identity and trusted conversation
session authorization, tenant/database sharing policy and production integration.
Local-only access is not counted as authenticated production ownership. ACL tests
use synthetic ASGI identities and do not verify a login system. No sharing/grant
endpoint is enabled. NOT RUN: live-provider/PostgreSQL and real browser visual QA.
Detailed boundaries are in DASHBOARD_STUDIO.md; older V6.4 statuses are unchanged.

### Phase 4 agentic dashboard composition (2026-10-31)

VERIFIED locally: conversation-first creation; independently governed source
collection; typed per-widget lineage; same-database/complete-source validation;
privacy-bounded design inputs; one targeted design correction with call ledger;
selected-widget preservation/replacement; failed required-goal rejection;
multi-source Save/restart and legacy v1 compatibility; scoped edits and unchanged
old snapshots; automatic frontend rendering without manual creation modes.
Evidence: test_multi_dashboard.py (11 cases), extended Studio/agent tests,
dashboard_view.cjs and dashboard_studio.cjs. Full regression: 605 Python passed,
2 skipped; 180 frontend/voice passed; Ruff, mypy and diff checks passed.

BLOCKED: external sharing until legitimate deployment identity, session ownership
and authorization integration exist. NOT RUN: live providers, PostgreSQL and real
browser visual QA. No production latency gain is claimed. Scope and call-count
measurements are recorded in V6_4_PROGRESS.md and AGENTIC_DASHBOARDS.md; older
requirement statuses remain unchanged.

### Human-reviewed dashboard design (2026-10-31)

VERIFIED locally: typed design proposals and source/layout requirements; the
existing Top-layer agent selects investigations and interprets explicit approval;
no conversational dashboard construction before exact-version confirmation;
natural-language revisions and rejection; unchanged layout-only source snapshots;
atomic proposal revision conflicts; restart/resume; missing-evidence continuation
through governed SQLite SQL followed by a new mapping review; failed-finalization
rollback and explicit retry; saved Studio compatibility/permissions; compact
discussion summaries and non-error frontend presentation.

Evidence: test_dashboard_proposals.py, extended test_multi_dashboard.py and
test_dashboard_studio.py, dashboard_studio.cjs and frontend_rendering.cjs. Existing
SQL/chart/security regressions remain enabled. Exact checks and counts are in
V6_4_PROGRESS.md; no older requirement status is promoted by this entry.

BLOCKED: production identity/session ownership and external sharing, as before.
NOT RUN: live-provider, PostgreSQL and browser visual QA. No cross-session
unsaved-proposal transfer or automatic refresh. Resume the original conversation
or authorized saved Studio. This approval-based workflow is historical; see
AUTONOMOUS_DASHBOARDS.md for the current validated-preview workflow.

### Live local dashboard completion (2026-11-01)

VERIFIED for one explicit consumption-only scenario: unchanged Llama 3.2
understanding plus Qwen 3.5 4B SQL/design; real `/api/chat` proposal with 21 complete
monthly rows; reviewed exact-version confirmation after API restart; READY with
the same result ID and 21-point line chart. Approval made no SQL/designer calls.
Temporary only: not saved or published. Native wire grammar/action consistency,
scoped reference validation and exact quoting retain canonical local validators.

Evidence: live API responses and persisted READY proposal, test_scoped_decisions.py;
641 Python passed/2 skipped, 182 frontend/voice passed, Ruff/mypy/diff passed.
This supersedes the earlier NOT COMPLETE outcome only for this tested scenario;
broad small-model accuracy, production auth/sharing and browser visual QA are
not verified. See V6_4_PROGRESS.md for failures, remaining redundant chart attempt,
timings and source boundaries. No model switch, commit or push.

### Compact design review UI — VERIFIED (automated)

Sources: `web/design_review.js`, `web/app.js`, `web/styles.css`, `web/index.html`.
Acceptance: `tests/design_review.cjs` and frontend progress regressions verify
explicit approval, historical approval disabled, no build on render, safe text,
terminal statuses, approval pause and unused stages. 186 frontend/voice tests
and 641 Python tests passed (2 skipped); Ruff/mypy passed. Browser appearance
remains unverified. Baseline commit `5ce519b`; these UI changes are uncommitted.
## Autonomous dashboard workflow — VERIFIED (automated, 2026-11-01)

Supersedes approval-before-build entries below; those describe historical work.
Sources: `api.py`, `conversational_agent.py`, `ai_dashboard_designer.py`,
`dashboard_studio.py`, `web/app.js`, `web/dashboard_studio.js`, `web/design_review.js`.
Acceptance: `test_dashboard_proposals.py` archival safety/requery/concurrency;
`test_multi_dashboard.py` automatic multi-result build/save/reopen;
`test_dashboard_studio.py` direct saved edits and snapshot preservation;
`test_ai_dashboard_designer.py` incomplete evidence and bounded validation;
frontend Studio/rendering/archive tests. 643 Python passed/2 skipped, 186
frontend/voice passed; Ruff/mypy/diff passed. No production/live latency claim.
External sharing, live-provider quality, PostgreSQL integration and browser visual
QA remain unverified or blocked by the pre-existing prerequisites.
### Ordinary analysis versus dashboard scope — VERIFIED (routing)

`conversational_agent.py`: explicit ordinary-query prompt/example and completion
scope consistency validation before tools. `ai_dashboard_designer.py`: optional
further analyses do not force requery; essential missing evidence still does.
Tests: plain trend with earlier dashboard context; invalid scope repaired once
before SQL; optional-suggestion design acceptance; automatic multi-result build.
Isolated live Llama routing: ask_data, dashboard_goal=null, finish_after_tool=true,
two model calls/zero DB calls, 8.66s. Full live SQL and browser verification were
not performed; running backend requires reload to adopt these code changes.
Checks: 645 Python passed/2 skipped; 186 frontend/voice passed; Ruff/mypy/diff passed.

Native-path follow-up VERIFIED: `AgentDecision.result_kind`, `_ollama_decision_actions`
artifact-first branches, and canonical scope validation. ANALYSIS/CHART cannot
select dashboard tools or set dashboard_goal; legacy decisions still parse.
Regressions cover native branch constraints, typed and legacy contradiction repair,
ordinary questions, and multi-result dashboards. 646 Python passed/2 skipped,
186 frontend/voice passed, Ruff/mypy/diff passed. Full isolated live consumption
query returned 21 rows in 16.34s, with one Top-layer and one SQL model call,
no dashboard call/retry. No state writes to the existing server; user declined
restart, so the running port-8001 process still needs manual reload.

Groq native-schema follow-up VERIFIED: inactive legacy proposal fields use
null-only scoped schemas, without invalid minimum/string constraints. Canonical
validation is preserved. Minimal live strict request accepted; isolated full
consumption query accepted with 21 rows, 2.40s, one Top-layer and one SQL model
call, no dashboard. Existing server was not restarted or modified.
Checks: 646 Python passed/2 skipped, 186 frontend/voice passed; Ruff/mypy/diff passed.

Bare-dashboard context reuse VERIFIED (scripted): Top-layer prompt and compact
accepted-cache fallback retain question/columns/semantic contract without a
ConversationState. New regression confirms one Top call, zero SQL calls and stable
result identity. Full suite: 647 Python passed/2 skipped. Live query returned 21
rows, but the subsequent live dashboard was rate-limited after a routing repair;
full live dashboard completion remains NOT VERIFIED. No running-server changes.

Three-layer BI design/editing VERIFIED (automated): `ai_dashboard_designer.py`
layered prompt, finite distribution/association evidence, scoped secondary result
profiles, enriched KPI definitions, preserved PATCH identities and unambiguous
ordering. `conversational_agent.py` retains governed edit scope; dashboard renderer
honors supported AI widths with separate chart rows. Tests prove candidate/filter
validation, differing scripted selections for same-schema/different observations,
replacement/removal/order and unaffected source preservation. 652 Python passed/
2 skipped, 187 frontend/voice passed; Ruff/mypy/diff passed. Live LLM design
quality and browser appearance are NOT VERIFIED; no new LLM or SQL calls added.

Four visible chart layers VERIFIED (automated): role metadata in DashboardWidget
and ChartChoice; new AI design schema/local checks require plot roles and a unique
Primary. Materialization orders Primary → Comparison → Distribution → Relationship;
PATCH preserves/reassigns roles explicitly. Typed spec/store compatibility and
frontend validate the same single-Primary invariant. Four sections render in order,
unsupported layers remain empty and no plots share a grid row. Existing legacy
unlabelled designs are preserved. Tests: 659 Python passed/2 skipped,
188 frontend/voice passed; Ruff/mypy/diff passed. Live new-role planning and browser
appearance remain unverified. Existing server and unsaved dashboards untouched.

Dashboard failure hardening VERIFIED (automated, 2026-11-02): typed safe diagnostics,
request/proposal/attempt logging without data, grounded year/label validation,
specific bounded repair with preserved valid choices, provider non-retry,
API/Studio messages and unchanged original revision after failure. Sources and
acceptance: DASHBOARD_VALIDATION.md; test_dashboard_design_diagnostics.py and
frontend message regressions. 677 Python passed/2 skipped, 189 frontend/voice
passed, Ruff/mypy/diff passed. Live-provider/browser/PostgreSQL verification not run.
Final source-scoped annotation regression included: 678 Python passed/2 skipped;
189 frontend/voice passed. Full results and exact changed files: DASHBOARD_VALIDATION.md.
