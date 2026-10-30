# Conversational visualization integration

The existing Top-layer LangGraph agent remains the only conversational
decision-maker. `ask_data` uses governed SQL generation; `refresh_analysis`
validates accepted historical SQL before read-only execution. A combined request
continues to `change_visualization` over that turn's accepted result. Missing
data uses the existing bounded REQUERY_REQUIRED continuation.

New chart designs use the existing visualization recommendation prompt and
LLMChartProposals schema. The bounded sample and column profiles now also carry
accepted grain, completeness and result identity. Chart proposals are validated
and materialized against actual rows, never executed as code.

Type-only edits to an accepted chart preserve X, Y, measures, series and size.
The existing `plan_chart` validator checks compatibility without a chart-model
call. Incompatible edits return UNSUPPORTED, not a silently changed metric.
Analytical changes still require governed SQL. Existing frontend chart choices,
colors, styles and zoom remain local; changing them invokes no SQL or LLM.
Natural-language requests still go through the Top layer.

Result IDs remain stable for chart-only turns; chart IDs identify new chart
specifications. Historical refreshes create new result IDs. Failed refreshes
cannot use unrelated old results as the source of a requested chart.

## Measured verification

Existing scripted SQL+chart → type-edit integration: SQL-generator calls remain
1; cumulative plotting-model calls decrease from 2 to 1. The initial design
still uses one plotting call; the edit uses zero. Top-layer calls are unchanged.
SQL-only continues without a plotting call. Historical lookup → refresh → chart
uses two Top-layer decisions and one chart call, with zero SQL-generator calls
after the original analysis.

Controlled 30-iteration two-row benchmark, with a scripted 5 ms model delay:

| Type-edit path | Chart calls | p50 | p95 |
| --- | ---: | ---: | ---: |
| Model path (controlled baseline) | 30 | 5.946 ms | 6.224 ms |
| Validated deterministic edit | 0 | 0.202 ms | 0.526 ms |

This measures the service path and simulated model delay, NOT live-provider or
end-to-end production latency. It does not establish token savings for a real
provider. Regression tests cover real SQLite historical refresh, combined
SQL/chart requests, multi-turn chart edits, bounded samples, column validation,
truncation, changed periods and rejected metric substitution.

## Integrated first-layer proposals

`AgentDecision.chart_proposal` is an optional existing `LLMChartProposal`, not a
new design schema. Native provider schemas include its complete nullable shape;
older responses may omit it in local parsing. Invalid decisions are repaired
through the existing bounded decision-repair path before any tool executes.

The Top layer can attach a provisional chart to `ask_data`, `refresh_analysis`,
or `change_visualization`. LangGraph request-local state preserves it across SQL
execution; the next SQL tool replaces/clears that proposal so it cannot silently
attach to an unrelated refinement. No proposal is treated as database evidence.
Existing complete results get advisory column profiles (no extra row samples).

After existing sufficiency checks, `validate_chart_proposal` validates against
actual result rows through `plan_chart`. It rejects unknown columns, invalid
numeric axes, duplicate grain, incompatible known unit families and silent
encoding substitution. Unknown units/business correctness are not independently
proven. Failed/truncated results cannot use this fast path. A rejected proposal
falls back to the existing LLM recommender; unavailable models retain the
existing validated deterministic fallback/terminal behavior.

Accepted proposals render through the same chart tool, result_id, fresh chart_id
and Plotly/ECharts path. No executable code is generated. Validation success or
rejection and elapsed time appear in the call ledger. Reconciliation preserves
the model's completion flag, avoiding a redundant final decision for a completed
chart-only request.

Scripted SQLite call counts (retrieval-only context, no recovery):

| Request | Top layer | SQL generator | Chart planner | Total |
| --- | ---: | ---: | ---: | ---: |
| SQL + chart, no proposal (baseline) | 1 | 1 | 1 | 3 |
| SQL + chart, valid integrated proposal | 1 | 1 | 0 | 2 |
| SQL + chart, incompatible proposal | 1 | 1 | 1 | 3 |
| Chart-only valid proposal over cached data | 1 | 0 | 0 | 1 |

`test_controlled_planning_latency_and_call_counts` reproduces a 30-iteration
service benchmark using a scripted 5 ms model delay: no-proposal p50 5.971 ms,
p95 6.592 ms; integrated proposal p50 0.353 ms, p95 0.546 ms. This excludes
Top-layer and SQL latency, is not live-provider performance, and does not measure
real token savings. The larger structured decision prompt may affect provider
latency/token costs. Context selection, repairs and investigation may add calls.
