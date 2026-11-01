# Human-reviewed dashboard design

The existing LangGraph assistant is the only conversational orchestrator. It
chooses relevant investigations, reuses accepted evidence, and delegates missing
analyses to the governed Text-to-SQL tools. `analyze_result` supplies deterministic
descriptive numerical evidence; SQL can retrieve distributions, trends and
multivariate inputs when required. This is not an automatic exhaustive scan of
every column, a causal-analysis engine, or another agent.

## Conversation

1. Ask for a dashboard and describe its business objective.
2. The assistant investigates within the existing request/tool/source budgets.
3. The bounded designer chooses complementary eligible KPIs/charts, order and
   widths, with objectives and reasons. A typed `DashboardDesignProposal` is
   retained, not displayed as a completed dashboard.
4. Queryflow asks: “Would you like to confirm this design, add more plots, or
   modify the proposed visualizations?” Describe additions, removals,
   replacements or rearrangements normally. Unaffected components remain intact.
5. Confirm the reviewed version explicitly. Only then does Queryflow validate and
   display the dashboard. **Save** and **Approve & publish** remain separate.

Changing a proposal invalidates its old approval/version. Silence, opening a new
conversation, and opening the Library never approve anything. Natural-language
approval is interpreted by the same Top-layer LLM, not another intent router.
The backend additionally requires an exact proposal ID/version and a verbatim
current-user approval statement. Changes and approval must be separate turns.

## State and persistence

`INVESTIGATING → PROPOSED → AWAITING_APPROVAL → APPROVED → BUILDING → READY`

Revisions record `REVISING → PROPOSED → AWAITING_APPROVAL`. `REJECTED`, `EXPIRED`
and `FAILED` are explicit terminal/repair states. A valid candidate's preparation
stages and version are recorded atomically after the bounded investigation/design
finishes; in-flight work uses existing progress telemetry and does not overwrite
the previous discussion. Cancellation or failed validation before that commit
leaves the prior proposal unchanged.

`dashboard_proposals.py` reuses the private Studio/history SQLite file. Additive
migration 3 creates session/database-keyed current discussions and versioned
history. Candidate documents contain independently validated retained snapshots,
SQL/provenance, semantic contracts, questions and immutable source IDs. Compact
summaries contain objectives, choices, reasons, widths/order, source requirements,
timestamps, bounded discussion and tool-calculated findings—never SQL or raw rows.
Only the candidate's explicit approval authorizes the existing revision/render path.

Atomic version predicates reject concurrent updates and stale approvals. Saved
edits additionally bind the saved dashboard ID/version and recheck existing Studio
permissions. Invalid revisions do not replace accepted proposals. Failed builds
retain their approved evidence and history. Approvals expire after 24 hours;
revision/review is required thereafter. This is an approval expiry, **not** proof
that a retained historical snapshot is current live data.

Reopen the original conversation after restart to resume its pending discussion.
Reopening a saved dashboard also displays its pending discussion for editors and
owners, not viewers. A different conversation does not implicitly inherit the
proposal or consent. Reset/Clear removes that conversation's pending discussion;
explicitly saved dashboards remain. There is no cross-session proposal transfer.

## Additional evidence and safety

Supported widgets retain per-result grain, columns, scope and lineage. Proposed
further analyses are separately listed as `missing_requirements`; they are not
presented as observed findings or fabricated charts. Approval cannot build while
they remain missing. The Top layer can gather the specified evidence through the
same governed SQL tools and submit revised verified mappings for confirmation.
New mappings require review again rather than silently broadening consent.

Layout-only revisions reuse snapshots; complete-proposal approval needs no SQL
or designer call. Existing SQL safety, KPI eligibility, chart validation, source
limits and request budgets remain mandatory. No global interactive filters,
automatic refresh, new chart types or autonomous agents were added. Internal
validated candidate specs are not Library entries or published dashboards.
Legacy explicit single-result dashboard APIs remain compatibility interfaces;
the conversational creation/edit path uses this approval gate.

External sharing remains disabled. Existing local-loopback/Studio permissions
are not a production login or tenant-authorization system. Session isolation is
preserved; a deployment must still supply legitimate identity/session binding.

## Verification and measured calls

### Local Ollama

Set `TEXT2SQL_DASHBOARD_PROVIDER=ollama` and
`TEXT2SQL_DASHBOARD_MODEL=qwen3.5:4b`, with the normal `OLLAMA_BASE_URL`.
The designer uses Ollama's native DashboardProposal response schema so small
models return a proposal instance rather than copying the prompt's JSON schema.
Unsupported grammar permits one bounded JSON-mode fallback when repair is enabled.
Local timeout defaults to 120s (API providers: 12s); override using
`TEXT2SQL_DASHBOARD_TIMEOUT_SECONDS`. The shared request deadline always wins.

Ollama also supports governed SQL generation now: set
`TEXT2SQL_SQL_PROVIDER=ollama` and `TEXT2SQL_SQL_MODEL=qwen3.5:4b`.
It reuses the same SQL prompt, validation, authorization and execution path.
Only local model IDs explicitly configured for an application role are admitted
to the model catalog. This configuration status does not prove the model is
downloaded or the server is reachable. SQL output truncation is a model failure,
not an executable partial query. No API key is required for a loopback Ollama server.

One live local synthetic-evidence test returned READY with two validated charts
in 12.94s, one model call and zero SQL/cloud calls. It is not a production latency
benchmark or a full business-database dashboard test. The previous JSON-mode
attempt echoed a schema and failed validation after 96.12s; cache/warmup conditions
were not controlled, so these runs do not establish a speedup ratio.

Subsequent real `/api/chat` tests on the debit-card database did **not** build a
valid dashboard with the current Llama 3.2 understanding/Qwen 4B SQL configuration.
Failures included repeated rejected tool actions, an invalid design, and a
zero-row result with an unrequested date predicate. The empty proposal was not
approved, saved or published. Passing scripted tests and the isolated designer
test are not evidence of end-to-end live model correctness. A model/configuration
change and another reviewed API test remain necessary.

**Later live verification (2026-11-01):** keeping Llama 3.2 for understanding and
Qwen 3.5 4B for SQL/design, the explicit consumption-only API request produced a
nonempty proposal with 21 monthly rows. After an API restart, exact confirmation
built a one-line-chart dashboard using the same result ID. Proposal request:
58.77s; successful confirmation: 4.86s, one Llama call, no SQL rerun or designer
call. It was not saved or published. This verifies one reviewed scenario, not
general model reliability or every chart/question.

The fix was integration-level, not a model switch: Ollama rejected large string
length bounds when compiling the decision grammar. Only large `maxLength` values
are omitted from its wire grammar; full Pydantic limits remain mandatory locally.
Native action/tool branches prevent contradictory output; current result IDs and
proposal references are constrained to scoped evidence. The confirmation quote
is constrained to the actual message, including punctuation. SQL handoff retains
the original client constraints alongside the scoped objective. Cloud provider
schemas and all SQL authorization/semantic guards remain unchanged.

### Scripted integration counts

Scripted-provider tests use real SQLite for governed analytical additions. These
are reproducible call counts, not live-model latency/accuracy measurements:

| Scenario | Top calls | SQL generation | Designer |
|---|---:|---:|---:|
| Three-result proposal | 4 | 3 | 1 |
| Confirm complete proposal, including after restart | 1 | 0 | 0 |
| Layout-only revision | 1 | 0 | 1 |
| Invalid revision with one bounded correction | 1 | 0 | 2 |
| Approve missing analysis, gather it, propose new mappings | 3 | 1 | 1 |

Compared with the previous automatic-build flow, the required separate approval
adds one Top-layer decision; it does not repeat SQL or design planning when the
evidence is already complete. No production speedup is claimed.

Acceptance evidence: `test_dashboard_proposals.py`, extended
`test_multi_dashboard.py` and `test_dashboard_studio.py`, plus Studio and frontend
rendering tests. Coverage includes no construction before approval, restart,
version conflicts, source isolation, rejected/expired/failed states, invalid
structured actions, layout-only reuse, new evidence and second confirmation,
unchanged saved snapshots, and discussion rendering without a chart-error label.
Live providers, PostgreSQL and real-browser visual QA require separate runs.

Implementation: `dashboard_proposals.py` owns typed discussions, expiry, versioned
storage and transitions; `conversational_agent.py` owns decisions and bounded
tool sequencing; `api.py` bridges the existing designer/build paths without new
SQL execution code. `ai_dashboard_designer.py` retains candidate validation and
adds per-choice reasoning. `dashboard_studio.py`/`dashboard_store.py` expose
authorized pending discussions while preserving existing saved documents.
`models.py` adds only additive response/private request metadata. Frontend changes
are confined to existing app/Studio presentation, CSS and cache-version tags.
