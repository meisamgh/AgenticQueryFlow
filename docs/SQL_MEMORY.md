# SQL-based conversational memory

The single Top-layer assistant remembers accepted analytical definitions, not
database rows. `search_conversation_memory` returns SQL, original/resolved
questions, semantic contract, database, execution timestamp and run/result IDs.
It returns no rows. SQLite expression indexes support exact identity and ordinal
lookups without deserializing the full conversation.

Canonical references are `first`, `latest`, `analysis:3`, `analysis:-2`, a run ID
or a result ID. Lookup enforces session, database, reset boundary and, when
branching, ancestry. Failed queries are not accepted memory. Inspection does not
replace the active result.

## Execution choices

- Chart-only changes reuse complete in-memory results and stable result IDs.
- `refresh_analysis` revalidates stored SQL against live schema, original allowed
  tables/columns and read-only policies through the existing checker/executor.
  It generates no new SQL. Refreshed values receive a new result ID and explicit
  current-data provenance, never the original execution's provenance.
- Modifications use `ask_data` with `source_run_id`, restoring that analysis's
  contract through the existing governed generation pipeline.
- Exact original figures require an actual retained snapshot. Explicit
  `inspect_result` with `snapshot:RUN_ID` exposes completeness; absent/incomplete
  snapshots cannot support numerical claims. No automatic refresh substitutes
  for original figures. No time-travel integration is implemented.

Commits remain delayed and version-checked. Failed refreshes preserve accepted
state; concurrent refresh commits conflict instead of silently overwriting.

## Retention and compatibility

Legacy UI history retains at most 20 preview rows, including chart data, rather
than full result sets. Set `TEXT2SQL_HISTORY_PREVIEW_ROWS=0` to retain no preview
rows. The memory tool never treats previews or summaries as numerical evidence.
Complete RAM results remain available for chart reuse. Legacy timestamps are
explicitly labeled record time when actual execution time was not retained.

## Local measurements

Same 250-record SQLite fixture, 40 lookups per implementation:

| Lookup | p50 | p95 | Serialized response |
| --- | ---: | ---: | ---: |
| Previous history scan/preview | 1.602 ms | 1.734 ms | 609 bytes |
| Indexed SQL memory | 0.090 ms | 0.098 ms | 442 bytes |

Both paths used zero model calls and zero business-database queries. These are
metadata microbenchmarks, not live-provider end-to-end improvements or tokenizer
measurements. A scripted refresh required two Top-layer decisions (lookup and
refresh), zero SQL-generator calls, and schema validation plus read-only SQL
execution. Live provider and PostgreSQL verification remain NOT RUN.
