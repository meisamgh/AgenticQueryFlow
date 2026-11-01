# Autonomous dashboard design

Dashboard creation no longer requires design confirmation. The existing Top-layer
LangGraph agent remains the only conversational orchestrator:

A bare "design a dashboard" follow-up inherits the last accepted analysis: its
question, result identity, metric, dimensions, filters and period. It does not
require the client to restate already-known requirements. The reasoner also
uses question/columns/contract from the accepted result cache when a separate
conversation-state object is unavailable. No summaries become numerical evidence.
Dashboard tools use source_result_ids; source_run_id remains null (reserved for
historical SQL modifications). Without accepted context, inspect available
metadata first and ask only a focused question for genuinely material ambiguity.

Objective → targeted evidence inspection → governed SQL if needed → bounded
dashboard designer → deterministic validation → immediate dashboard preview.

Ordinary analytical questions do not create dashboards. Investigations follow the
business objective, not an exhaustive column scan or fixed chart-role template.
The agent can use existing numerical analysis tools and retain up to four complete
analytical results. Missing evidence returns `REQUERY_REQUIRED` to that same agent;
invalid designs stop with a structured failure rather than an approval request.

## Designer prompt

The active `_PROMPT` in `src/semantic_text2sql/ai_dashboard_designer.py` uses the
Senior BI Analyst / Exploratory Data Scientist / Dashboard Design Architect
instructions supplied for this revision. It evaluates observed behavior,
business relevance, information value, complementarity, interpretability,
evidence quality and visual suitability. It selects exact eligible candidate IDs
and returns the existing `DashboardProposal` JSON, never executable chart code.

The name `DashboardProposal` is the specialist's structured output type, not a
human approval state. Exact source identities, metric definitions, grain, axes,
completeness and chart compatibility remain deterministically validated. One
bounded designer correction is allowed within the shared request budget.
Essential missing requirements cannot be presented as a finished design. Optional
`further_analyses` are suggestions, not authority to expand the client's objective
or trigger additional SQL automatically.

Layout remains verified scope/filters → eligible KPIs → charts → optional data.
Charts occupy full-width rows; KPI cards may share a row. Aim for three useful
KPIs only when supported; no invented metrics, baselines or decorative trends.

## Editing and persistence

Natural-language edits apply validated changes immediately to an unsaved preview.
PATCH preserves unaffected widgets and snapshots. Presentation-only changes reuse
evidence; missing measures/dimensions use governed SQL without refreshing old
source snapshots. Undo, Versions, Save, Save as New and Approve & Publish retain
their existing explicit document-management behavior. No automatic publication,
external sharing, source refresh or interactive global filtering is enabled.

Legacy proposal tables/migrations and serialization remain readable. Pending
designs are archival: they do not seed new evidence, authorize builds or appear as
active Studio discussions. Historical chat cards are read-only with no approval
controls. Reset retains its existing scoped history-clearing behavior.

## Measured checks

Scripted integration tests (not live-provider latency benchmarks):

| Scenario | Previous Top/SQL/designer calls | Automatic Top/SQL/designer calls |
|---|---|---|
| Three-analysis dashboard, including confirmation | 5 / 3 / 1 | 4 / 3 / 1 |
| Saved layout/component edit, including confirmation | 2 / 0 / 1 | 1 / 0 / 1 |
| Missing evidence → requery → design | — | 4 / 2 / 2 |
| Saved dashboard reopen | 0 / 0 / 0 | 0 / 0 / 0 |

Regression sources include `test_multi_dashboard.py`, `test_dashboard_studio.py`,
`test_dashboard_proposals.py` (archival compatibility and automatic requery),
`test_ai_dashboard_designer.py`, and frontend Studio/dashboard rendering suites.
Live model quality, PostgreSQL infrastructure and browser pixel-level appearance
must be verified separately; offline test timings are not production speedups.

Final local verification: 643 Python passed, 2 skipped (21.50s), 186 frontend/voice
passed (515ms), Ruff, mypy (65 source files), and `git diff --check` passed.
The concurrency regression inserts a competing revision while model work is
awaited and verifies that it is preserved rather than silently overwritten.

### Ordinary questions remain ordinary analyses

The Top-layer prompt explicitly distinguishes a metric/trend question from a
dashboard request, even when an earlier dashboard is in context. Its first
structured example is SQL-only with no dashboard goal and completion after the
accepted result. A separate combined SQL+chart example remains supported. No
keyword-based competing router or extra classification model was added.

The designer is instructed to omit default/unselected fields and keep rationales
brief within the configured completion budget; token limits are not increased.

Live diagnostic (isolated, local Llama, no SQL or persistence): the first prompt-only
check chose ask_data but incorrectly attached dashboard_goal while marking the
request complete after SQL. The new deterministic completion-scope consistency
guard rejects that contradiction before tools and uses the existing one-repair
budget. Recheck: ask_data / NEW_QUERY / dashboard_goal=null /
finish_after_tool=true, two actual model calls, zero DB calls, 8.66s. This is not a
full live SQL/dashboard benchmark or proof of general local-model accuracy.

### Native routing follow-up: complete query path verified

A subsequent real history record showed the same contradiction surviving both
local validation attempts. Constraining completion alone was insufficient: the
model could choose an ongoing dashboard goal and repeat the SQL. The Top-layer
decision now includes optional typed `result_kind` (ANALYSIS, CHART, DASHBOARD).
Older scripted/provider decisions remain compatible through a null default.
For native Ollama, artifact kind is decoded first: ANALYSIS/CHART branches forbid
dashboard tools and require null dashboard_goal; only DASHBOARD permits that
workflow, and cannot falsely claim completion after its first SQL. Canonical
post-decision validation enforces the same scope rules. No keyword router or
second model/agent was introduced.

Isolated full live `/api/chat` test with current configured providers and the
real debit-card database: "Total consumption over date" returned accepted SQL,
21 rows, one successful Top-layer call and one successful SQL-generation call,
zero designer calls or retries, 16.34s. SQL groups yearmonth.Consumption by Date
and orders by Date. History was isolated in memory; the port-8001 server and
its accepted state were not restarted or modified. This single live test does
not establish general model reliability or production latency percentiles.
