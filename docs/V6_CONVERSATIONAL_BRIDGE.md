# V6 conversational bridge

The newer top-layer assistant is described in
[V6.2 conversational assistant](V6_2_CONVERSATIONAL_ASSISTANT.md). This document
describes only the earlier deterministic metadata bridge, still available as a
separate fallback when the assistant is disabled.

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
