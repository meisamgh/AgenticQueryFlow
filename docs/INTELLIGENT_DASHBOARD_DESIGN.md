# Intelligent BI dashboard creation and editing

The existing Top-layer LangGraph agent investigates the objective and retrieves
missing evidence through governed tools. The existing designer LLM selects how to
communicate it. No new agent, router, framework or SQL execution path was added.

## Configuration and diagnostics

`TEXT2SQL_DASHBOARD_PROVIDER` and `TEXT2SQL_DASHBOARD_MODEL` select the
existing designer adapter; empty values inherit the request's assistant model.
`TEXT2SQL_DASHBOARD_MAX_TOKENS` bounds the completion. Credentials belong in
local configuration, never documentation or saved dashboards.
See [validation diagnostics](DASHBOARD_VALIDATION.md) for structured failures
and bounded correction, and [permanent Studio](DASHBOARD_STUDIO.md) for saving,
revisions, ownership and persistence. The deterministic compatibility API and
KPI eligibility rules remain documented in [Dashboard MVP](DASHBOARD_MVP.md).

## Three decision layers

1. **Filters/scope:** select exact verified filter refs. These control displayed
   metadata only, not SQL predicates. Applied source constraints always remain.
2. **KPIs:** select validated IDs using their definitions, units, period metadata,
   aggregation and grain. No minimum count or invented metrics to fill slots.
3. **Visualizations:** display Primary, Comparison, Distribution and Relationship
   in that order. Exactly one selected plot is Primary. Other sections contain
   useful evidence-supported plots; an unsupported section remains clearly empty,
   never filled with a fabricated plot. Compare combinations for nonredundancy.

The active `_PROMPT` is in `src/semantic_text2sql/ai_dashboard_designer.py`.
Output remains the existing `DashboardProposal`: exact candidate IDs, PATCH/
REPLACE, filter_refs, kpis, charts, remove_ids, theme and bounded missing evidence.
The application validates and renders immediately; Save and Publish stay explicit.

## Evidence available to the specialist

Profiles remain bounded to the first 24 result columns with no sample rows or
category literals added to the prompt. Numerical summaries now include finite
observation count, mean, population standard deviation, quartiles and IQR outlier
count. Quartiles use linear interpolation; outliers use 1.5 × IQR fences.
Categorical concentration uses observed frequency counts without exposing values.
NULL/non-finite values are excluded from numerical calculations and reported.

At most four non-identifier numeric columns supply at most six pairwise Pearson
associations, requiring at least three paired observations. Constant measures
return an undefined coefficient, not a fabricated zero. Evidence is descriptive
at the complete returned SQL grain: not causal, not database-wide, and not proof
of statistical significance. No cross-result join or correlation is invented.

Multi-result context labels each source independently; the anchor references its
existing profile rather than duplicating it. Other retained sources get separate
behavior summaries. Incomplete/inconsistent rows are rejected before profiling.
Previous KPI titles, identities, metrics and calculation types remain available
for editing. Validated KPI definitions are included in the candidate catalog.

## Editing

The same Top-layer agent distinguishes scope/metric changes from presentation,
KPI, plot/encoding, layout and investigation requests. Presentation-only edits
reuse accepted evidence. Missing analytical requirements return to the governed
pipeline; the designer never runs SQL itself.

PATCH retains all unmentioned widgets and snapshots. Replacements keep the target
widget ID; removals are explicit. Conflicting order positions are rejected rather
than silently interpreted. Applied SQL constraints cannot be removed by hiding
filter metadata. Existing source/grain/KPI/chart validators are unchanged.

AI chart widths now honor the supported 4/6/8/12 spans, with full width preferred.
Each chart starts at column one of a separate row. Mobile charts remain full
width; KPIs may share a row. Deterministic/legacy default rendering stays full
width. Plotly/ECharts and existing CSS tokens are reused.

New AI designs must assign an analytical_role to each plot; schema, materializer
and persisted spec validation enforce complete labeling and a single Primary.
PATCH preserves existing roles unless explicitly changed. Removing the Primary
requires promoting a replacement. Older unlabelled documents remain compatible
without guessed reclassification. Role annotations are planner judgments, not
proof of causal or statistical claims; existing chart/source validation remains.

## Evaluation boundary

Scripted-provider tests verify different validated selections for identical
schemas with different outlier behavior; this tests evidence propagation and
selection plumbing, not actual LLM judgment. Other tests cover finite statistics,
constant correlations, filter IDs, KPI definitions, replacements/removals/order,
multi-source lineage, zero-SQL layout edits, bounded repair and saved compatibility.
No live model benchmark or browser pixel-level verification was run for this change.

Local context-only microbenchmark (101 runs, four-row monthly/country fixture):

| Measurement | Before | After |
|---|---:|---:|
| Median context construction | 0.0268 ms | 0.0468 ms |
| Serialized context JSON | 2,453 bytes | 3,321 bytes |

Richer evidence adds local work and context bytes; this is not a latency-speedup
claim or a model-token measurement. It adds no SQL or LLM calls, and remains
within existing result/model/tool budgets. Production latency and model selection
quality require separate evaluation.

Final checks: 652 Python tests passed, 2 skipped (21.47s); 187 frontend/voice tests
passed (509ms); Ruff, mypy (65 source files), `git diff --check` passed.
Primary changed implementation files: `ai_dashboard_designer.py`,
`conversational_agent.py`, `web/dashboard_view.js`, `web/styles.css`,
`web/index.html`. Acceptance coverage is in `test_ai_dashboard_designer.py`,
`test_multi_dashboard.py`, and `dashboard_view.cjs` alongside the existing suites.
Earlier uncommitted work was preserved; no environment/model changes or publication.
