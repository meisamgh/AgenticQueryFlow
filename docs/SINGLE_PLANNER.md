# Single conversational planner

Implemented locally on 2026-11-04 against parent `87de8be`. This refactor removes
two independent model stages, not the SQL generator or conditional recovery.

```text
Question → Top-layer LangGraph agent (intent + optional typed chart proposal)
         → Hybrid retrieval → Deterministic semantic / optional graph grounding
         → SQL generator → SQLGlot + semantic checks → Read-only execution
         → Deterministic chart validation / materialization → Plotly / ECharts

Failure / unexpected empty or NULL / correctness investigation
         → Existing bounded recovery + evidence tools → Repair or inform
```

## Boundaries

- `service.py`: removed Model 1 invocation and candidate relationship copies used
  only by that invocation. Hybrid retrieval, dependency restoration, glossary/KPI
  grounding, graph gating, verification, semantic contracts and recovery remain.
- `conversational_agent.py`: the existing `AgentDecision.chart_proposal` is the
  sole LLM chart-planning boundary. Proposals can accompany SQL or an existing
  result. Typed sufficient decisions retain the existing no-extra-reasoning path.
- `plotting_service.py`: accepts only an accepted, complete result with a stable
  ID, validates actual encodings and materializes locally. Invalid/missing
  proposals use the existing deterministic planner. Missing analytical dimensions
  still return `REQUERY_REQUIRED` to the Top layer; no SQL is run by the chart tool.
- Chart-type changes preserve accepted encodings; explicit UI styling remains
  local. No competing natural-language routing agent was added.
- Routine result observations contain IDs, completeness and compact column
  profiles, not row previews. Profiles include types, roles, cardinality, NULL
  counts and measure ranges; identifier ranges and raw category values are omitted.
  Full SQL/rows remain available through existing authorized inspection tools.
- The legacy non-Top API path no longer calls a chart model either.
- The separate dashboard designer is unchanged and can still call its model.

## Compatibility and configuration

`ChatRequest.context_mode="model1"`, `context_provider`, `context_model` and the
service constructor's `context_completers` are accepted for old callers but do
not activate a model. UI configuration returns retrieval and null retired model
fields. The frontend no longer requires a configured context model to become ready.

The legacy functions `plan_context_detailed` and `refine_charts_with_model` remain
deterministic compatibility adapters. They never call their supplied completer.
The retired visualization feature flag always reports false. Their old prompts
and model-call implementations were removed.

`.env.example` no longer advertises context/plotting providers, models or dedicated
completion-token settings. Existing local `.env` files were not edited: retired
settings are ignored. Configure the assistant and SQL roles normally. Existing
conditional recovery may reuse an underlying model; three distinct models are
not required. Restart a running server to load code changes.

Telemetry reports deterministic grounding separately. `model1_latency_ms` remains
zero for response compatibility, planner token usage remains empty, and no retired
stage appears as a new MODEL ledger entry. The inspector can still read historical
model ledger entries; new grounding/chart validation timings are tool timings.

## Measured comparison

Same scripted FastAPI/SQLite benchmark against the parent archive and working
source: `tests/test_single_planner_paths.py -k benchmark`. Ten measured runs per
scenario after one unmeasured warmup, independent sessions and identical data.
Retired stages deliberately sleep 5 ms and report fixed 100 input / 20 output
tokens per call. Top decisions report fixed 10 / 5 tokens; the SQL stub does not
report tokens. This measures local orchestration, **not live-provider performance**.

The parent is explicitly requested in legacy Model 1 mode. If a deployment was
already retrieval-only, it already avoided the Model 1 call in the SQL-only case.

| Scenario | Total model calls before → after | DB calls before → after | p50 ms before → after | p95 ms before → after |
| --- | --- | --- | --- | --- |
| SQL only | 3 → 2 | 2 → 2 | 38.754 → 32.668 | 43.844 → 79.900 |
| SQL + chart, no provisional proposal | 4 → 2 | 2 → 2 | 45.688 → 36.357 | 46.863 → 50.985 |
| Plot accepted result | 2 → 1 | 0 → 0 | 30.699 → 25.090 | 36.337 → 27.644 |
| Change chart type | 1 → 1 | 0 → 0 | 25.108 → 24.507 | 35.307 → 25.312 |

DB counts are schema inspection plus execution, taken from the current-turn call
ledger. SQL-generator calls are also counted independently. SQL/plot scenarios
preserve the exact fixture outputs `[5.0, 30.0]`; chart-only turns preserve result
identity. All 40 measured runs in each version satisfied these assertions.

Retired-stage input/output tokens per turn were respectively 100/20, 200/40,
100/20 and 0/0; all are now zero. These are fixed scripted usage reports, not
production tokenizer measurements. Total SQL-turn tokens are unknown because the
SQL stub reports none; no production token-reduction percentage is claimed.

All requested charts passed validation in this benchmark. Without a provisional
proposal, the new SQL+chart and cached-plot fixtures deliberately use deterministic
fallback in 10/10 runs; chart-type edits retain accepted encodings. Separate tests
cover valid and invalid Top proposals, zero model budgets and missing source IDs.
No live SQL execution-accuracy, business-semantic accuracy or subjective chart
quality score was measured. Passing fixtures do not establish those metrics.

The 30-run chart-service microbenchmark also measured missing-proposal p50/p95
5.946/6.490 ms before vs 0.199/0.266 ms after; calls 30 → 0. Valid Top proposals
already took zero independent model calls before this change.

Tail latency did **not** improve uniformly: SQL-only and combined p95 increased
in the small API sample. Measurements contain OS, SQLite/profile I/O and Python
scheduling noise. Larger repeated live-provider trials are required before any
production latency/quality claim.

## Verification and files

Python coverage includes first-turn SQL+chart, cached plots, type edits, invalid
proposals, requery, same-turn refinement, history isolation, semantic dependencies,
bounded recovery, request budgets and profile privacy. Frontend coverage retains
chart rendering, fallback, history, tab switching, controls and progress behavior.

Production changes: `.env.example`, `README.md`, `service.py`, `context_planner.py`,
`conversational_agent.py`, `plotting_service.py`, `visualization_recommender.py`,
`api.py`, `models.py`, `llm.py`, `web/app.js`, `web/index.html`.

Tests added/updated: `test_single_planner_paths.py`, `test_integrated_chart_planning.py`,
`test_context_planner.py`, `test_semantic_graph.py`, `test_visualization_recommender.py`,
`test_v6_intelligence.py`, `test_api.py`, `test_conversational_agent.py`,
`test_v63_conversation.py`, `test_sql_memory.py`.

Final checks: 725 Python tests passed, 2 PostgreSQL tests skipped (19.24s);
198 frontend/voice tests passed (619ms); Ruff, mypy (70 source files) and
`git diff --check` passed. `frontend_rendering.cjs` also verifies the new tool-stage
timings and absence of unexecuted plotting-model stages.

No new dependencies, migrations, provider credentials, agents or SQL execution
paths were introduced. Graph Phase 0 limitations and production authorization
limitations are unchanged. Live-provider, real PostgreSQL and browser pixel QA
were not run; PostgreSQL tests remain skipped without their DSNs.
