# Queryflow V6.4 tool consolidation checkpoint

The Top-layer LangGraph assistant remains the only conversational decision-maker.
It chooses an existing governed tool; no new intent router or investigation agent
was introduced. `ask_data` still owns schema retrieval, grounding, SQL generation,
SQLGlot/semantic validation, read-only execution, bounded recovery and accepted
state. Chart changes still go through the existing plotting sufficiency gate.

| Requested capability | Current interface | Contract and boundary |
| --- | --- | --- |
| Search business context | `search_business_context` plus compatible `get_business_context` / `get_available_metrics` | Approved glossary/catalog definitions and at most five profiled value candidates. A sampled or stale profile is **not** live value proof; no `DISTINCT` query runs. |
| Inspect database | Existing `list_tables`, `search_schema`, `describe_table`, `get_relationships`, `get_column_profile` | Allowlisted `VerifiedSchemaSnapshot` is shared by metadata tools within one assistant turn; a new turn inspects again. |
| Execute analytical query | Existing `ask_data` | Only the governed Text-to-SQL service executes analytical SQL. The Top layer receives a bounded result observation; it cannot supply arbitrary SQL. |
| Inspect accepted result | `inspect_result` | Accepts `result_id`, `offset` and `limit` (1–20); returns bounded cached rows, completeness, SQL, the accepted contract and descriptive statistics when complete. A mismatched ID fails closed. No SQL executes. |
| Manage visualization | Existing `change_visualization` | Uses the plotting service, result-sufficiency validation, and chart/result identities. |
| Explain analysis | Existing `explain_existing_result`, `analyze_result`, `present_result` | Uses accepted SQL/result evidence; no new SQL unless the Top layer separately requests `ask_data`. |
| Investigate | Existing `check_sql_correctness` and bounded recovery | Reuses the existing recovery coordinator. No second autonomous agent was added. |

The new `ToolInput` fields are `result_id: str | None`, `offset: int = 0` and
`limit: int = 10` (maximum 20). They are included in the provider-native
`AgentDecision` schema; older locally validated decisions remain compatible
through defaults. `search_business_context` returns typed value candidates with
table, column, value, exact/fuzzy/approved-alias match, provenance and ambiguity.
It never silently rewrites the client's literal. Live exact existence remains
the responsibility of the bounded recovery/value-probe path.

## Measured local comparison

`benchmarks/benchmark_metadata_tools.py` runs the same scripted three-decision
turn against the previous revision (`591c606`) and this implementation, using
a real temporary SQLite schema and no provider network. Two metadata tool calls
(`list_tables`, then `search_schema`) previously inspected the schema three
times: `conversational_bridge.py::inspect_metadata` inspected once per call,
and `search_schema` inspected a second time inside the second call.
`conversational_agent.py::_reason` also repeated long policy prose on each of
the three decisions; the policy was compacted without changing the decision
schema or ownership.

| Same metadata question, 30 sequential turns | Before | After |
| --- | ---: | ---: |
| Schema inspections per turn | 3 | 1 |
| Prompt characters across three model calls | 22,289 | 18,293 |
| Model calls / mock input+output tokens | 3 / 30+15 | 3 / 30+15 |
| Governed SQL calls | 0 | 0 |
| Local p50 / p95, one run | 12.78 / 16.76 ms | 12.69 / 16.14 ms |

The prompt-character reduction is measured; it is **not** a measured provider
token or cost reduction. Repeated local latency runs varied, so no robust
end-to-end latency gain is claimed. A separate scripted SQL-question benchmark
still uses two Top-layer calls and showed no improvement from this change.
The full Python suite passed with one existing skip; 72 Deno frontend tests,
Ruff and mypy over 55 source files passed. `git diff --check` also passed.

## Remaining work

- `inspect_result` currently addresses the **active accepted result**, not an
  arbitrary historical result ID. Historical restore must occur before inspection.
- Business-context value matching is bounded profile retrieval, not live exact
  existence. Semantic embedding/BM25 value ranking and a freshness-aware value
  index were not added; that requires a benchmark and data-version policy.
- Multi-step investigation continues to use existing recovery. A separate
  Investigation Agent has not been justified by measured failures.
- Live-provider token usage, PostgreSQL, semantic correctness on a held-out
  corpus, and production p95 are not established by this local benchmark.
