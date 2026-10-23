# V6 conversational bridge

## Experimental adaptive assistant

`TEXT2SQL_CONVERSATIONAL_AGENT_ENABLED=true` enables a separate, opt-in LangGraph
reason/act loop above the same governed `/api/chat` SQL path. It is disabled by
default. Configure `TEXT2SQL_ASSISTANT_TIMEOUT_SECONDS` (default 60),
`TEXT2SQL_ASSISTANT_MAX_MODEL_CALLS` (default 4), and
`TEXT2SQL_ASSISTANT_MAX_DATABASE_CALLS` (default 4). The selected request model
is the orchestration model unless `TEXT2SQL_ASSISTANT_PROVIDER` and
`TEXT2SQL_ASSISTANT_MODEL` override it. Provider credentials remain in the existing
provider configuration. The assistant can answer greetings and chart-type changes through
safe fast paths, choose metadata and analytical tools, and use up to three tool
steps. It delegates data questions to the existing Text-to-SQL chat handler;
the agent itself never runs model-authored SQL.

The request-local LangGraph state holds the current decision, up to three compact
tool observations, token usage, call ledger, and eventual response. Accepted
analytical state remains in `ConversationStore` and `RunHistoryStore`; unsuccessful
metadata or agent responses do not replace accepted SQL. The tool capability
names are `list_tables`, `search_schema`, `describe_table`, `get_relationships`,
`get_column_profile`, `get_available_metrics`, `get_business_context`, `ask_data`,
`analyze_result`, `check_sql_correctness`, `change_visualization`,
`present_result`, and `explain_existing_result`. Schema and profile access is
limited to the request's configured database. Analytical tools call the existing
chat service, not a direct database connection.

This is an **experimental integration**, not the complete agent described by the
V6 design. In particular, delegated SQL calls still have their own inner
request budget, and result explanations and visualization recommendation may
still use separate models. Tool schemas, multi-user authorization, analytical
state consolidation, and evidence-grounded multi-step RCA need further work
and frozen A/B evaluation before enabling it for shared deployment. The older
`TEXT2SQL_CONVERSATIONAL_BRIDGE_ENABLED` path remains available independently.

This is the first, opt-in integration seam between the V6 conversational-agent design and the
existing production Text-to-SQL workflow. It is not a replacement SQL engine or a claim that the
complete V6 design is implemented.

```text
/api/chat (TEXT2SQL_CONVERSATIONAL_BRIDGE_ENABLED=true)
  -> bounded LangGraph route
       schema question   -> existing allowlisted database inspection
                       -> existing hybrid schema retrieval when columns/relationships matter
       glossary question -> approved GlossaryStore only
       analytical question -> existing chat handler, unchanged
                           -> TextToSQLService -> SQL model -> validation/execution/recovery
```

The bridge is in `src/semantic_text2sql/conversational_bridge.py`. Its `MetadataToolInput` and
`MetadataToolOutput` are validated Pydantic contracts. Tool evidence IDs are stable hashes of the
tool, database and returned facts. Metadata routes do not call a language model or issue arbitrary
SQL. Schema inspection has one database-call allowance and a ten-second deadline. The tool uses
only databases already allowed by `DatabaseRegistry` or `PostgresRegistry`; a missing database is
rejected. Historical SQL and sampled values are not exposed by these metadata routes.

The SQL branch calls the existing chat handler directly, without internal HTTP. Existing session
state, accepted SQL, bounded execution, provider configuration and conditional recovery remain
authoritative there. Metadata questions are saved in run history but do not replace accepted SQL
or the accepted semantic contract. The bridge is disabled by default so the V5 behavior can be
compared against it without changing existing callers.

## Enable

```dotenv
TEXT2SQL_CONVERSATIONAL_BRIDGE_ENABLED=true
```

Restart the server after changing `.env`. The existing web UI and `/api/chat` request/response
shapes remain unchanged. The test suite includes a schema question that bypasses SQL generation,
an approved glossary definition, an unknown definition that is not invented, and an analytical
question that still invokes the established SQL pipeline.

## Not yet implemented

This bridge does not add the V6 LLM tool-selection loop, a new `AnalyticalState`, deterministic
result analysis tools, combined LLM explanation/chart composition, or multi-step investigations.
Those require separate implementation and frozen V5/V6 evaluation. The current graph deliberately
has only high-confidence metadata routes plus the existing SQL path; ambiguous requests stay with
the established conversation resolver.
