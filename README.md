# Semantic Text-to-SQL

A conversational Text-to-SQL application for SQLite and allowlisted PostgreSQL databases. The
normal path retrieves and grounds a compact schema, generates read-only SQL, and executes it.
Failures, empty or unexpected `NULL` results, and explicit correctness reviews can activate a
bounded evidence-gathering recovery agent. In assistant mode, a top-layer model chooses bounded
tools, the governed Text-to-SQL engine supplies query results, and a separate plotting model
may propose a chart over those accepted rows. Chart proposals remain deterministically validated.

<p align="center">
  <img src="Arch-pic" alt="Semantic Text-to-SQL architecture" width="1000" />
</p>

_Visual overview of the query workflow. The architecture and runtime boundaries documented below
are authoritative._

## Demo

https://github.com/user-attachments/assets/99608fc4-5d31-419d-af3f-6cd446d09372

## Architecture

The [V6.2 conversational assistant](docs/V6_2_CONVERSATIONAL_ASSISTANT.md) is enabled by
`TEXT2SQL_CONVERSATIONAL_AGENT_ENABLED=true` (set in `.env.example`). When enabled, its
top-layer model decides the action for every conversation turn, including greetings, schema
questions, plot follow-ups and analytical questions. Bounded tools ground factual answers;
the chart-reusability gate and SQL safety checks remain deterministic. Analytical
questions still use the same governed Text-to-SQL engine; the assistant cannot execute SQL
directly. Follow-up `ask_data` calls now carry the top model's resolved objective and typed
changes into a provisional semantic contract. The contract is accepted only with a successfully
validated and executed query. A plotting specialist operates on complete accepted result rows:
it can propose type, axes and formatting but cannot execute SQL or invent data. If a requested
dimension or time grain is absent, the result-reusability gate requires a new governed query.
Set `TEXT2SQL_PLOTTING_PROVIDER` and `TEXT2SQL_PLOTTING_MODEL` to route plotting separately;
they default to the selected request model. The plotting call shares the request-wide model
budget. Set the assistant flag to `false` to retain the older path. The earlier
[V6 bridge](docs/V6_CONVERSATIONAL_BRIDGE.md) remains available as a separate fallback.
The assistant is not yet recommended for multi-user production deployment; see the documented
limitations and evaluation requirements.
V6.3 orchestration is still being hardened: the executable SQLite conversation tests cover
total-to-grouped follow-ups, one-turn SQL plus plotting, chart-type reuse, and missing-grain
rejection. This does **not** establish full business-semantic correctness or live-provider
reliability. PostgreSQL and provider integration require separately configured credentials.
Local cold/warm and transport measurements, including their limitations, are
documented in [Performance](docs/PERFORMANCE.md).

```text
Question -> conversation resolver
                 |
                 v
Hybrid schema retrieval (BM25 + dense + value matching -> RRF)
                 |  optional Model 1 context selection
                 v
Deterministic grounding (keys, join paths, grain, types, date formats, glossary)
                 |  optional, strongly matched historical SQL examples
                 v
Selected SQL model -> SQLGlot safety -> EXPLAIN -> read-only execution
                 |
       +---------+--------------------+
       |                              |
 ordinary result               failure / anomalous result /
       |                       explicit correctness review
       v                              |
 validated chart selection             v
       |                       bounded recovery agent
 validate chart against rows          | inspect_schema / inspect_values /
       |                       | query_database -> REPAIR / INFORM / ESCALATE
 top-layer model explains result       |       |
       |                               +-- focused SQL retry when justified
       v
 SQL + exact rows + answer + optional chart + context + attempt/usage history
```

The default context method is retrieval-only. Users may select optional Model 1 context selection
and choose the SQL model separately. Model 1 selects context; it does not write SQL. Historical SQL
examples are disabled by default and, when enabled, are admitted only after compatibility checks.
The legacy path can still use the selected SQL model for chart choice and result explanation.
In assistant mode the top-layer model explains the validated result. Plot requests can invoke
the independent plotting model; a repeated validated chart does not require another model call.
A chart is never evidence that SQL is business-correct.
Clicking a chart already recommended for the accepted result is a display-only switch: it
changes the current plot in place and does not create a new question or execute SQL. Requests
for a different, unvalidated chart design still enter the conversational assistant.

Contract follow-ups are applied as typed changes: adding a dimension updates both grouping
and output grain; adding a filter preserves existing constraints; ambiguous filter removal
or replacement asks for clarification instead of silently editing unrelated predicates.
The active period is separate from comparison periods. An explicit assistant reset clears
conversation state and cached results without generating SQL. Semantic SQL checks reject
provable missing outputs, aggregations, grouping, and filter values; formula equivalence,
join fanout, and full business correctness are **not** proven by these checks.
The **New conversation** button calls a deterministic reset endpoint; it does not need a
model response and does not delete saved history. **Clear** also deletes that session's
saved history.
The SQL validator now records high-confidence AST findings for required output aliases,
aggregation presence, grouping presence, and missing required filter literals. Provable failures
enter bounded repair; uncertain formula equivalence and filter predicates remain explicitly
unresolved rather than being presented as verified business correctness.

### Stakeholder answers and charts

In the legacy presentation path, the selected model may propose `bar`, `line`, `scatter`, or
`none` from the question, accepted SQL and up to 40 returned rows. Code verifies the proposed
columns, numeric values, date axis and row grain; invalid choices become `none`, not another
guessed chart. A second model call then explains the result using that validated choice. The UI
renders only actual returned rows and never runs model-generated chart code. SQL and the exact
result table remain available in the SQL and Data tabs.

Interactive charts use Plotly.js for supported chart types, with ECharts retained as a fallback.
Time-series plots include drag/wheel zoom, a touch pinch gesture, and a range-slider navigator
when there are enough points. Chart export uses the active renderer. The plot receives the
validated result rows without browser-side aggregation or dropping missing values.

Each chat can contain multiple independent analytical questions. Follow-ups, corrections,
explanations, filters, and plots use the latest accepted query when relevant; a new question starts
fresh query state within the same chat, without inheriting the previous SQL or chart.
The inspector separates verified context, schema, recovery trace, measured pipeline timing,
and token usage into dedicated tabs.
The browser requests up to 500 read-only result rows, which the server holds in bounded RAM for
later "plot it" follow-ups. In assistant mode, a visualization-only follow-up can use those rows
with a bounded plotting-model call, without regenerating SQL. Results exceeding the
row limit are not plotted as if complete. Saved history retains only a 20-row preview, so a server
restart requires rerunning the question before plotting it.
For an explicit plot request with more than two returned columns, the selected model may propose
chart axes from the executed result
(including a validated Year+Month display axis). Proposed charts are checked against actual
columns and rows; when the user names specific plot columns, the chart uses only those columns.
The optional chart model sees column roles and at most five sample rows, never the full result.
It cannot create measures or aggregate rows for display. Two-column results use the
deterministic chart planner without an extra model call. Model recommendation is enabled by
default for wider explicit plot requests and can be disabled with
`TEXT2SQL_VIZ_RECOMMENDER_LLM_ENABLED=false`.

Both calls share the request-wide model-call budget and have a 15-second timeout each. Their token
usage and outcomes appear separately in the call ledger. A model failure or exhausted budget leaves
the query result available with a factual fallback. Partial results are labelled, and explanations
are interpretations of returned data—not correctness proofs. Recovery and human-review messages
take precedence over this presentation path.

### Conversational analytics planner

Visualization follow-ups ("plot it by month", "compare Germany and France", "remove France") do not
re-run the full SQL pipeline blindly. The accepted conversation state (semantic contract, last
question, accepted SQL, stored columns/rows/grain) produces a **deterministic analytics plan first**
(`analytics_planner.plan_analytics`). A **Result Reusability Gate**
(`data_requirement_validator.validate_reusability`) then decides, from metric identities,
grouping, time grain and stored-grain capability, whether the stored result can be reused as-is,
needs new validated SQL, or needs clarification. Aggregates never grow finer grain client-side and
non-additive metrics (averages, ratios, distinct counts) are always recomputed in SQL.
Filter, metric, aggregation and comparison changes also require new SQL. Missing requested
metrics or incomplete saved rows cannot silently become a reused chart.

**Governed derived metrics** (for example Profit Margin, Average Order Value) are recognized only
when their definition is approved in the semantic catalog; the plan carries their lineage as an
`ADD_MEASURE` change and the gate forces recomputation through validated SQL. The approved formula,
source relation and required fields are passed as generation evidence on this path; this is not
a formal proof that the resulting SQL is formula-equivalent. Formulas without
an approved definition are never invented — unverified formula requests fall to clarification.

When the deterministic rules leave the request ambiguous (`plan_needs_refinement`), an optional
**flag-gated LLM analytics refinement** runs one bounded model call
(`refine_plan_with_model`). The model sees the previous question, accepted SQL, result columns and
the deterministic draft plan, and returns a typed JSON plan proposal that is re-validated with
Pydantic, checked against verified schema (`plan_fields_known`) and against the approved metric
catalog. Any invalid, timed-out, schema-inventing or unauthorized output falls back to the
deterministic plan. Obvious chart mechanics (reuse, chart-type changes) never reach the model.
Optional planner and recommender calls use the request-wide model-call and deadline budget, with
their reported tokens included in response accounting.

Executed results also get ranked chart recommendations. The deterministic recommender scores
candidate charts against real rows and column roles/units; an optional **flag-gated LLM
visualization recommender** (`refine_charts_with_model`) adds business-aware proposals
(multi-measure comparisons, series selection, alternatives) as structured JSON. Every proposal is
re-validated through the same deterministic chart planner (`plan_chart`); fabricated columns,
unrenderable combinations and invalid JSON are dropped, and the deterministic ranking stays
authoritative on failure.
For a plot request without explicitly chosen axes or chart type, the validated primary
recommendation controls the displayed chart. Explicit user choices are preserved.

Planner decisions (source, operation, reusability decision, latency) are recorded per turn in a
bounded local ledger exposed at `GET /api/analytics-metrics`; decisions are metrics only, never row
data.

Filter value discovery (`GET /api/databases/{db_id}/filter-values`) is fail-closed: the field must
exist in the live schema as a non-key text/date column, must not be marked sensitive by verified
profile metadata, and high-cardinality fields are answered with a bounded server-side `search`
rather than presenting the first values as the complete domain.
Search input is bound as a SQL parameter and treated literally, including quotes and `%`/`_`.
The process-local cache is capped at 256 searches with a 60-second lifetime; it is not a
multi-user authorization boundary.

### Client workspace

- Inspect selected tables and columns in the Query Inspector after asking a question.
- Record a voice question with configured Qwen audio or the Groq Whisper fallback, review its transcript, and send it as text.
- Choose the database in the top bar and see connection status and session ID there. SQL and context
  model routing is configured in `.env`; the question composer has no model selectors.
- Open the drawer-style inspector for context, schema, recovery evidence and execution details.
- The Context inspector follows the flat Queryflow prototype layout while showing only current
  response facts; the Schema tab shows selected tables, columns and verified relationships.
- Reopen saved conversations with their SQL, result previews, attempts, context and chart proposals.
- Type corrections in the main composer and inspect SQL versions, filter changes and structural comparisons.
- Expand recovery history to inspect recorded tool observations, evidence IDs and REPAIR / INFORM / ESCALATE decisions.
- Use per-message Answer, Data and SQL tabs. Answer opens by default; switching tabs reuses the
  existing response without generating or executing another query. SQL keeps highlighting, copy
  and version details, with Check correctness and Optimize actions for that specific SQL run;
  Data shows the executed rows and CSV export. The separate saved-question and branch buttons
  have been removed; conversation history remains available.
- See a model-selected chart and plain-language answer when the returned data supports them.
- For a complete chart result, switch among compatible recommended plots in the answer without another SQL call. Long chronological series
  have zoom controls; chart zoom never changes the underlying query result.
  Explicit plot requests allow up to 500 bounded result rows; larger results are flagged as
  truncated and are not plotted. Missing line/bar measures remain visible as gaps, never zeros;
  stacked and composition charts reject missing measures. An axis change that would collapse
  separate rows requires a new validated query instead of silently dropping data.
  Chart warnings remain visible beside the chart; the SQL and Data tabs show the source query and rows.
- Ask for filter, metric, or grouping changes in a conversational follow-up. The visible filter and
  parameter panels have been removed; the existing SQL generation, validation, and execution path
  handles such changes. Charts with three or more category points support touch pinch zoom,
  including horizontal bars. Zero matches show an empty view, while Source Data and the accepted SQL remain unchanged.
- Follow live backend stages and a timestamped activity log, including recovery schema/value
  checks and read-only probes. Timings are observations, not predicted completion percentages.
- Cancel a running request; the configured database and model remain fixed during execution.

Set `TEXT2SQL_DEFAULT_DB_ID` in `.env` to choose the web app's database. The database
selector is not shown in the UI. For SQLite, `TEXT2SQL_DATABASE_ROOT` must contain
`<db_id>/<db_id>.sqlite`; profile JSON files do not replace the database files.
Frontend rendering regressions can be checked with `node --test tests/frontend_rendering.cjs`.

## Agent-first recovery

The agent is deliberately absent from successful routine queries. It activates only when additional
reasoning and evidence are useful:

```text
Generated SQL
    |
    v
Validate + execute
    |
    +-- ordinary result --------------------------> return
    |
    +-- SQL failure / zero rows / unexpected NULL
    |                                                |
    +-- explicit correctness review                  v
                                             Recovery agent
                                                  |
                                         choose one bounded tool
                                  /             |             \
                         inspect_schema  inspect_values  query_database
                                  \             |             /
                                           verified observation
                                                   |
                                            typed evidence claim
                                                   |
                                      deterministic evidence policy
                                                   |
                                              reason again
                                                   |
                                      REPAIR / INFORM / ESCALATE
```

`inspect_schema` returns only approved structural metadata: selected tables and columns, physical
and semantic types, keys, grain, relationships, NULL presence, date formats and temporal coverage.
It does not return sampled categorical values as proof. The deterministic `inspect_values` tool
checks identifier and category existence in `EXACT` mode; its separate `SEARCH` mode returns only
advisory candidates and cannot prove existence or absence. The more general `query_database` tool
is reserved for narrow analytical probes that value inspection cannot answer.

The model chooses what it needs to inspect, but it never controls authorization. SQLGlot parsing,
SELECT-only enforcement, database and table allowlists, timeouts, row limits and the three-tool-call
ceiling are mandatory code-level controls. The agent never receives a database connection and never
silently substitutes an explicit user value.

Every failure and tool observation receives a stable evidence ID. Before `REPAIR` or `INFORM` is
accepted, the agent must emit typed claims that cite IDs from that request's evidence ledger.
Deterministic policy rejects missing, malformed or invented citations; data-grounding and
correctness claims must cite live tool evidence rather than relying only on the failure message.
It also enforces evidence compatibility: schema claims require matching schema output, value claims
require a matching exact-value probe, filter mismatches require a zero-count probe, NULL causes
require an observed NULL, and join effects require differing before/after counts.

The three terminal decisions are:

| Decision | Meaning |
|---|---|
| `REPAIR` | Give Model 2 a focused, evidence-backed correction instruction |
| `INFORM` | Explain a verified empty, NULL or otherwise noteworthy result |
| `ESCALATE` | State what remains uncertain without guessing or changing the request |

## Supporting capabilities

- Conversational new queries, refinements, corrections, explanations and optimization requests
- SQLite discovery and allowlisted PostgreSQL connections
- BM25, local dense embeddings and profiled value matching fused with Reciprocal Rank Fusion
- Optional LightGBM column reranking with deterministic fallback to RRF
- PK/FK, formula-dependency and bridge-table restoration
- Compact verified context instead of full-schema prompting
- Mandatory observed format for every selected date/datetime column
- Advisory categorical examples and observed date formats in the compact model context
- SQL-only generation with at most three total attempts
- Syntax-coloured SQL, tabular results, progress, cancellation and token accounting
- Result-equivalence and performance gates for explicit optimization requests
- Evidence-based correctness review and human-in-the-loop escalation
- Versioned governed semantic catalog with approval-record status model and evidence-backed verification probes

## Recovery modes

The bounded LangGraph recovery agent operates in five modes:

| Mode | Trigger | Purpose |
|---|---|---|
| `FAILURE` | SQL/schema/data execution failure | Gather focused evidence for repair |
| `FILTER` | Unresolved explicit filter value | Verify storage without silent substitution |
| `ZERO_RESULT` | Successful execution with no rows | Distinguish a valid empty result from filter/join mismatch |
| `NULL_RESULT` | Result contains unexpected SQL `NULL` | Inspect filters, joins, aggregation and missing-value evidence |
| `CORRECTNESS` | User requests correctness review | Compare an independent evidence-based candidate |

Before SQL generation, categorical values and temporal metadata are advisory context rather than
hard validation rules. Sampled values never prove that another value is invalid. Data validity is
investigated only after an execution anomaly or an explicit correctness review.

Zero-row and unexpected-NULL diagnoses are displayed once as informational messages. Users may
provide a correction naturally in the normal chat, but the agent does not present a separate edit
form or silently modify the request.

## Validation boundary

Before execution, generated SQL must:

- Parse successfully with SQLGlot
- Contain exactly one statement
- Be `SELECT` or `WITH ... SELECT`
- Contain no writes, DDL, administrative commands or `SELECT INTO`

SQLite uses query-only connections. PostgreSQL uses read-only transactions, server-side database
allowlists and bounded timeouts. A query that executes is reported as executable—not automatically
business-correct. Returning rows is never treated as proof of correctness.

## Models

The API supports four project-scoped provider transports:

- AgentRouter: `gpt-5.6-sol`, `glm-5.3`, `deepseek-v4-flash`, `claude-opus-5`,
  `claude-opus-4-8`
- Groq: `qwen/qwen3.6-27b`
- JustDoWork: `gpt-5.6-sol`, `claude-opus-4-8`
- CodeCraft API: `claude-opus-5`; set `CODECRAFT_API_KEY` in `.env`. The default
  endpoint is `https://codecraftapi.com/v1`. CodeCraft charges by token usage; check
  your account's available credits before querying.

Only configured models are available in the interface. Credentials remain server-side and are
never returned to the browser. JustDoWork has no hard-coded endpoint: set both
`JUSTDOWORK_API_KEY` and `JUSTDOWORK_BASE_URL=https://api.justwoker.icu`.
The Anthropic-compatible client sends requests to `/v1/messages`.

## Quick start

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/meisamgh/semantic_text2sql_ideal.git
cd semantic_text2sql_ideal
uv sync --dev --extra ml
cp .env.example .env
```

Add the required provider key to the ignored `.env`, then run:

```bash
uv run uvicorn semantic_text2sql.api:app \
  --host 127.0.0.1 \
  --port 8000 \
  --env-file .env
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Check the API with:

```bash
curl http://127.0.0.1:8000/api/health
```

## Voice questions

Click the microphone beside the question box, speak, then click stop. The configured audio provider transcribes
the recording into editable text. Review the transcript and press Send to ask the database.
Existing text is preserved; recording never submits a query automatically.

The app uses Qwen audio when configured. Otherwise it uses the existing `GROQ_API_KEY`
with Groq's `whisper-large-v3-turbo` transcription endpoint. To choose Qwen instead,
configure its audio workspace separately in `.env`:

```dotenv
QWEN_AUDIO_API_KEY=your-audio-api-key
QWEN_AUDIO_BASE_URL=https://YOUR_WORKSPACE_ID.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1
QWEN_AUDIO_MODEL=qwen3.8-omni-flash
```

Use your workspace's actual Chat Completions URL and a key from the same region;
see the [Qwen-Omni setup guide](https://www.alibabacloud.com/help/en/model-studio/qwen-omni).
`DASHSCOPE_API_KEY` and `DASHSCOPE_BASE_URL` also work. The SQL provider is selected separately.
Recordings are limited to 60 seconds and converted to mono 16-kHz WAV in the browser.
Audio is sent through the server to the selected provider for transcription, without being saved by this app.
The browser needs microphone permission and localhost or HTTPS. Discard cancels recording
or transcription. When the provider supplies token usage, it is reported separately below the
composer and by `POST /api/voice/transcribe`; `GET /api/voice` reports configuration availability.
If Qwen audio is not configured, `GROQ_API_KEY` enables the
[Groq Whisper transcription endpoint](https://console.groq.com/docs/speech-to-text) automatically.

## Database configuration

SQLite databases are discovered below `TEXT2SQL_DATABASE_ROOT`:

```text
data/
  books/
    books.sqlite
```

PostgreSQL databases are configured as a server-side JSON allowlist using read-only credentials:

```dotenv
TEXT2SQL_POSTGRES_DATABASES={"analytics":"postgresql://reader:password@localhost/analytics"}
```

Generate an offline profile with:

```bash
uv run python scripts/profile_database.py \
  --dialect sqlite \
  --db-id books \
  --output profiles
```

Important settings:

| Variable | Purpose | Default |
|---|---|---|
| `TEXT2SQL_DATABASE_ROOT` | SQLite database root | `data` |
| `TEXT2SQL_DEFAULT_DB_ID` | Database used by the web app | `debit_card_specializing` |
| `TEXT2SQL_POSTGRES_DATABASES` | PostgreSQL database allowlist | unset |
| `TEXT2SQL_PROFILE_ROOT` | Offline profile root | `profiles` |
| `TEXT2SQL_GLOSSARY_ROOT` | Approved glossary root | `data/business_glossaries` |
| `TEXT2SQL_SEMANTIC_CATALOG_ROOT` | Governed semantic catalog root | `data/semantic_catalogs` |
| `TEXT2SQL_RETRIEVAL_TABLES` | High-recall table budget | `5` |
| `TEXT2SQL_RETRIEVAL_COLUMNS` | Approximate columns per table | `5` |
| `TEXT2SQL_SCHEMA_RERANKER_ENABLED` | Enable LightGBM column reranking | `true` |
| `TEXT2SQL_HISTORY_ENABLED` | Enable strongly matched historical examples | `false` |
| `TEXT2SQL_SQL_PROVIDER` | SQL provider used by default for chat requests | `groq` in `.env.example` |
| `TEXT2SQL_SQL_MODEL` | SQL model used by default for chat requests | `qwen/qwen3.8-27b` in `.env.example` |
| `TEXT2SQL_CONTEXT_MODE` | `model1` or `retrieval` for chat requests | `retrieval` |
| `TEXT2SQL_CONTEXT_PROVIDER` | Provider for Model 1 when enabled | selected SQL provider unless overridden |
| `TEXT2SQL_CONTEXT_MODEL` | Model for Model 1 when enabled | selected SQL model unless overridden |
| `TEXT2SQL_REQUEST_TIMEOUT_SECONDS` | Shared end-to-end request deadline | `180` |
| `TEXT2SQL_REQUEST_MAX_MODEL_CALLS` | Maximum model-call budget per request | `6` |
| `TEXT2SQL_REQUEST_MAX_DATABASE_CALLS` | Maximum database-call budget per request | `24` |
| `TEXT2SQL_CONVERSATION_STORE` | Optional SQLite path for durable conversation state | in memory |
| `TEXT2SQL_CONVERSATION_TTL_SECONDS` | Durable/in-memory session lifetime | `86400` |
| `TEXT2SQL_JOB_TTL_SECONDS` | Completed asynchronous-job lifetime | `3600` |
| `TEXT2SQL_MAX_JOBS` | Maximum retained asynchronous jobs | `500` |

See [.env.example](.env.example) for provider-specific settings. Never commit `.env`.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | Health check |
| `GET /api/models` | Model discovery and configuration state |
| `GET /api/databases` | Configured database discovery |
| `POST /api/chat` | Synchronous conversational query |
| `POST /api/chat/jobs` | Start a cancellable query |
| `GET /api/chat/jobs/{job_id}` | Retrieve progress or result |
| `DELETE /api/chat/jobs/{job_id}` | Cancel an active query |
| `POST /api/check` | Parse supplied SQL; execution requires administrative enablement |
| `GET /api/databases/{db_id}/semantic-catalog` | Governed semantic catalog composed with live schema status |
| `GET /api/databases/{db_id}/semantic-catalog/verification` | Evidence-backed grain, quality, coverage and freshness probes |

Example:

```bash
curl -X POST http://127.0.0.1:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "demo-1",
    "db_id": "books",
    "message": "Count the books by category.",
    "provider": "agentrouter",
    "model": "gpt-5.6-sol",
    "context_mode": "retrieval"
  }'
```

Conversation state is in memory by default. Set `TEXT2SQL_CONVERSATION_STORE` to retain versioned
state in SQLite across API restarts. Completed conversation turns are saved separately to
`data/runtime/query_history.sqlite` by default (`TEXT2SQL_RUN_HISTORY_PATH`; use `:memory:`
to disable persistence). The history keeps up to 500 turns with at most 20 preview rows per turn.
Stored rows are snapshots, labelled with their saved time and preview count.

Opening history calls no model and executes no business SQL. **Continue from here** restores
the selected turn's accepted context. **Rerun saved SQL** revalidates that exact saved statement
against its table/column scope and current schema, executes it read-only, and records a new run.
This is separate from asking the SQL model to regenerate an answer.

History routes are scoped by the browser session ID:

```text
GET    /api/history?session_id=...
GET    /api/history/{run_id}?session_id=...
GET    /api/history/{run_id}/turns?session_id=...
POST   /api/history/{run_id}/continue?session_id=...
POST   /api/history/{run_id}/rerun?session_id=...
DELETE /api/history?session_id=...
POST   /api/sql/diff
```

Each turn has stable conversation, turn and run IDs. Recovery presentation events are built
from actual attempts and observations by backend code; an agent claim is labelled as reported,
not promoted to a verified fact. The SQL diff compares syntax trees and shows changed filters
and unchanged/changed SQL structure. These are inspection aids, not semantic correctness proofs.

## Semantic catalog

Databases may carry a governed, versioned semantic catalog document
(`data/semantic_catalogs/<db_id>.json`). It declares entities, natural grain,
time semantics, quality contracts, join provenance and business metrics with an
explicit status model: `observed | inferred | reviewed | approved | stale |
conflicting`. Machine-authored facts land as `inferred` and can never promote
themselves; `approved` facts require a human `approval_source: human_review`
record, are re-validated against the live schema on every read, and a failed
probe surfaces them as `conflicting` or `stale` instead of silently trusting them.

`GET /api/databases/{db_id}/semantic-catalog` composes the document with live
schema state; `/verification` runs the read-only probes (relation existence,
declared column match, grain uniqueness, required null checks, positive measures,
time coverage) and returns evidence-backed claims.

## Verification

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy src
git diff --check
```

The automated suite checks software behavior; it is not a claim of Text-to-SQL execution accuracy.
Model quality must be measured on a frozen dataset, database state, provider, prompt and
result-equivalence protocol. The optional browser-rendering smoke test requires Node.js:

```bash
node --test tests/frontend_rendering.cjs
# With Deno installed, run both browser-rendering and voice tests:
deno test --allow-read tests/frontend_rendering.cjs tests/voice_input_test.mjs
```

## Project structure

```text
src/semantic_text2sql/
  api.py               FastAPI endpoints and web delivery
  conversation.py      turn classification and session state
  run_history.py       durable, scoped conversation turns and bounded result snapshots
  sql_diffs.py         SQL version comparisons and observed filter changes
  recovery_events.py   typed presentation events from actual recovery observations
  voice.py             bounded Qwen/Groq speech transcription
  hybrid_retrieval.py  BM25, embeddings, values, RRF and ML reranking
  context.py           deterministic grounding and context assembly
  llm.py               provider adapters and generation prompts
  validator.py         SQLGlot syntax and read-only safety checks
  recovery.py          bounded LangGraph evidence recovery
  presentation.py      chart-first model calls, chart checks and result interpretation
  runtime.py           shared request deadline and model/database call budget
  agent.py             generation, repair and execution orchestration
web/                   conversational interface
tests/                 unit and integration tests
```

## Scope

This repository is suitable for controlled analytics pilots where database access, providers and
business definitions are governed. It is not presented as unrestricted autonomous production SQL,
and it does not claim semantic correctness solely from successful execution.
