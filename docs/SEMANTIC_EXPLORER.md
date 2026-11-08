# Semantic Explorer

Open **Semantic Explorer** in the sidebar. Schema, KPI dependencies and Combined
views reuse the existing NetworkX snapshot, compiler, approved metric resolution
and join resolver. Cytoscape 3.30.4 (MIT) is vendored locally and loaded on demand.

Product scope: only `debit_card_specializing` is exposed. Other configured databases
remain usable by Text-to-SQL but are rejected by every explorer graph/detail route
and omitted from its source list. The underlying graph implementation is reusable.

The debit-card catalog contains four glossary-derived candidate metrics: total
consumption, total spending, average unit price and weighted average unit price.
Candidates display as suggested metadata with non-authoritative edges; this does
not promote them to approved contracts for SQL grounding. Physical units, currency
conversion, annual growth definitions and human approval are not inferred.

The KPI overview shows source tables by default. `SOURCE_TABLE` links are display
summaries of existing required-column lineage, not new joins or approved business
relationships. Individual columns remain expandable; upstream lineage retains the
full column-level evidence. Summary links honor node, edge and relationship filters.

Search metadata, select nodes/edges, expand or collapse neighborhoods, fit/zoom/pan,
inspect approved formulas and source columns, or trace upstream lineage and potential
downstream impact. The accessible node list provides keyboard access. Select 2–8
tables to check join connectivity and bridge requirements. Inferred links remain
suggestions, forbidden links are red, and impact is not proof of a runtime effect.
Accepted-result and context-inspector links open the same explorer without changing
accepted analytical state. Positions and viewport are retained in page memory per
datasource and view, not across reloads.

## Read-only API

All routes under `/api/semantic-graph` are GET-only:

- `/sources`: configured datasource IDs.
- Root, `/neighborhood`, `/lineage`: bounded metadata graphs.
- `/nodes/{node_id}`: safe selected metadata and related approved KPIs.
- `/connect`: existing deterministic join-path resolution.

Parameters include `db_id`, `view`, `node_id`, `depth`, `node_limit`, `edge_limit`,
`kinds`, `relations`, `direction` and `search`. Maximums: 250 nodes, 500 edges,
depth 6, 5,000 examined links, 20,000 schema columns and 256 catalog metrics.
Tables initially hide columns; expand or search to load bounded neighborhoods.
Truncation and stale-catalog warnings are explicit. Invalid/stale approved metrics
are omitted, not presented as verified. Metadata identity invalidates the small
16-entry index cache. Graph builder version 2 invalidates older compiled identities.

No LLM calls, business-row queries, SQL generation, raw graph fact dumps, sample
values, approval identities, credentials or private cache paths are returned.
Schema inspection still uses the existing registry metadata mechanism. Responses
are private/no-store; the in-process metadata index is reusable.

## Authorization boundary

Enabled by default; opt out with `TEXT2SQL_SEMANTIC_EXPLORER_ENABLED=false`.
Existing loopback-only workspace checks and datasource allowlisting apply.
Cross-site and forwarded requests are denied. Remote authenticated identities are
also denied: datasource/tenant permission integration is not available, so external
production sharing is BLOCKED rather than trusting arbitrary session IDs.

## Verification (2026-11-08)

946 Python tests passed, 2 PostgreSQL integration tests skipped; 218 frontend/voice
tests passed. Ruff, both mypy invocations (75 source files) passed. Explorer coverage
uses real SQLite metadata, approved/inferred/stale catalogs, lineage, bounds,
composite keys, forbidden joins, cache invalidation and authorization. Business-row
execution is forbidden in the API fixture and the scripted model receives zero calls.
Frontend tests exercise actual vendored Cytoscape headlessly, safe text rendering,
view changes, expansion/collapse, stale responses, datasource isolation and layout.

Synthetic in-process TestClient benchmark: eight-node/11-edge lineage, 5,585 JSON
bytes; cold request 11.80 ms; 100 warm requests p50 1.09 ms, p95 1.22 ms.
These are local metadata timings, not production latency or a before/after speedup.
Desktop Chrome confirmed Schema/KPI rendering and approved formula details. Full
mobile/dark-mode visual QA, large production graphs and live remote transports were
NOT RUN. Responsive, reduced-motion and dark styles have automated source coverage.

Implementation: semantic_explorer.py, semantic_explorer_api.py, semantic_graph.py,
api.py, web/semantic_explorer.js/.css, web/app.js, web/index.html and vendored assets.
Tests: test_semantic_explorer.py and semantic_explorer.cjs.
