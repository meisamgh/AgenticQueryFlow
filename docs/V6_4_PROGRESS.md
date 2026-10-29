# V6.4 progress checkpoint

October 28 unified-tool/performance checkpoint:

- The Top-layer remains the sole conversational decision-maker. Existing governed
  SQL, plotting, explanation and recovery interfaces were retained. Added
  `search_business_context` for approved definitions plus advisory profiled value
  candidates, and `inspect_result` for bounded inspection of the active accepted
  result by exact result ID. Neither tool executes arbitrary SQL.
- Metadata tools now reuse one request-local verified schema/profile snapshot.
  A scripted two-tool turn measured one schema inspection instead of three.
  The compacted Top-layer prompt measured 18,293 characters across three calls
  versus 22,289 previously; live token savings are unverified.
- Focused regressions cover snapshot reuse, exact/fuzzy profile-candidate
  provenance, no live value scan, accepted-result pagination and stale-ID
  rejection. Full-suite and frontend verification are recorded in
  [UNIFIED_TOOLS.md](UNIFIED_TOOLS.md) and the implementation report.
- This does not complete V6.4. Arbitrary historical result-ID inspection,
  freshness-aware categorical indexes, full semantic equivalence, production
  authorization and live-provider/PostgreSQL benchmarking remain open.

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

### Session memory continuation (local checkpoint)

- The existing run-history store now exposes compact `SessionMemoryTurn` references: question, bounded assistant summary, accepted/failed status, result/chart IDs, column names, row count, and a small contract summary. SQLite reads project only those JSON fields; they do not load full saved rows or SQL into the Top-layer prompt.
- The Top-layer reads at most three recent same-session/database turns once per request and can call `search_conversation_memory` for older references. The existing Top-layer remains the only conversational decision-maker. Historical summaries are explicitly advisory; result IDs and accepted SQL remain the evidence boundary.
- Reset starts a durable memory epoch without deleting audit history. Explicit historical restoration can still use the chosen parent lineage. Tests verify session/database isolation, persistence across reopening, and one-call compact prompt construction.
- Historical numerical comparisons use bounded, authorized snapshots rather than re-executing old SQL. Full unrestricted historical result sets are intentionally not exposed. No before/after live-provider latency or semantic-correctness improvement is claimed.
- Historical snapshot inspection is now implemented. `inspect_result` accepts an exact authorized historical `result_id` through `RunHistoryStore.authorized_result_record`, subject to same-session/database scope, reset epoch, accepted/completeness checks, and optional parent lineage. It returns only the bounded saved snapshot and provenance; SQL is never re-executed. The Top-layer marks the observation as historical so it cannot silently replace the current result.
- Targeted verification: 68 frontend tests, full Python suite (one skip), Ruff, mypy, and `git diff --check` all pass. The synthetic memory benchmark measured 0.092 ms p50 / 0.095 ms p95 search latency and 822 bytes for the bounded recent context. Live-provider and PostgreSQL measurements remain unverified.
- Business-context matching now uses a bounded profile-backed categorical index keyed by database, dialect, and profile timestamp. Candidates carry source table/column, complete-versus-sampled provenance, profile freshness, and an explicit fuzzy ambiguity reason. This remains advisory: sampled/profile values are not live existence proof and are not silently promoted to SQL filters. Existing approved glossary aliases and definitions remain authoritative.
- Replaced the chart proposal prompt with the requested expert business-analytics visualization-planner instructions, retaining the application's JSON schema and bounded result sample.
- Verification: focused visualization/V6.3 tests, full Python pytest (one skipped), 72 Deno frontend tests, Ruff, mypy over 55 source files, and `git diff --check` passed. No live-provider call was made; improved chart quality in live use remains unmeasured.
