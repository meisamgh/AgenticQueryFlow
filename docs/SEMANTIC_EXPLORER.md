# Semantic Explorer

Read-only Database & KPI Knowledge Graph Explorer over the existing NetworkX
snapshot and locally vendored Cytoscape.js. No new graph engine, metric registry,
agent, LLM call or analytical SQL execution.

## Current interface

KPI Lineage opens by default when KPI definitions exist; otherwise Database Schema
opens. Combined uses bounded, focus-centered exploration. Cards have semantic
accents: purple KPIs, blue tables, teal columns, green dimensions and amber rules.
Labels remain plain text; decorative SVG backgrounds contain no metadata.

Opening KPI view preselects and inspects a KPI and expands its actual inputs.
Select a table in schema view to expand columns and HAS_COLUMN links. Expanded
columns retain their table endpoints. Dependencies, filters and dimensions appear
only when present in the catalog; no synthetic business relationships are added.
The debit-card catalog has four inferred metrics and no KPI-to-KPI dependencies.

The inspector exposes formula, description, source, required columns, units,
aggregation, catalog version and expandable metadata. Suggested definitions are
explicitly labeled candidate/not approved in the inspector. This does not change
catalog approval or SQL grounding. Edge labels appear on hover, selection or the
selected node's connected edges; FK cardinality is shown only from metadata.

Search highlights matches. Native zoom, pan, dragging, keyboard-accessible node
selection, collapse and view-specific position retention remain available.
Collapsed **View options** contains Fit, Refresh, Reset and bounded node/edge
filters. Small screens can toggle the inspector. Themes and reduced motion use
existing QueryFlow tokens; no global redesign or framework was introduced.

## Completeness and evidence

- `truncated` means node, edge or traversal-response limits were reached.
  `truncation_reasons` identifies those limits.
- `collapsed_nodes` / `collapsed_edges` count intentionally omitted metadata
  when no response limit was reached. Hidden columns do not trigger truncation.
- `total_nodes` / `total_edges` describe available filtered metadata, not just
  the visible neighborhood. Search totals refer to the bounded match set.
- Approved status derives from current authoritative metric evidence. Business
  rules do not become approved merely because of node type.
- Definition links are non-authoritative references. They cannot alone certify
  approved lineage. Labels distinguish approved, suggested, mixed and unavailable
  lineage; forbidden relationships never confer authority.
- KPI source-table summary links are display-only rollups of required-column
  lineage. They never enter join resolution or mutate the source snapshot.
- Stale/incompatible catalogs are withheld. Candidates with missing physical
  source columns are omitted rather than showing incomplete provenance as usable.

## API and security

GET-only routes under `/api/semantic-graph`: root, `/sources`,
`/neighborhood`, `/lineage`, `/nodes/{node_id}` and `/connect`.
All configured local datasource IDs use the existing registry allowlist.
Unknown sources/nodes, cross-site requests and forwarded remote access are rejected.
Remote authenticated users are also denied until genuine datasource/tenant
authorization is integrated. Session IDs are not proof of ownership.

Maximums: 250 nodes, 500 edges, depth 6, 5,000 examined links, 20,000 schema columns
and 256 catalog metrics. A 16-entry graph-identity cache invalidates on metadata
changes and isolates datasources. HTTP responses are private/no-store. API graph
filter parameters remain supported; no accepted conversation state is modified.
Tests forbid registry business execution and ConversationStore writes.

No credentials, sample business rows, approval identities or private cache paths
are serialized. Graph exploration uses registry metadata inspection only.
Enabled by default; opt out with `TEXT2SQL_SEMANTIC_EXPLORER_ENABLED=false`.
Graph persistence, KPI grounding and local ontology recovery also default on;
these flags do not enable remote sharing. SQL and recovery architecture are unchanged.

## Verification — 2026-11-09

Full run: 952 Python passed in 35.97 seconds,
2 PostgreSQL integration tests skipped; 232 frontend/voice passed. Ruff, mypy
(75 source files) and diff checks passed. Tests exercise actual SQLite metadata,
approval transitions, lineage direction, composite keys, forbidden joins,
collapsing, counts/limits, search, caches, stale replies and safe text rendering.

Synthetic metadata TestClient benchmark, 100 warm requests, 8 nodes / 11 edges:
cold 13.03 ms; warm p50 1.32 ms, p95 4.38 ms; JSON 5,659 bytes.
Previous comparable small-graph run: 12.52 ms cold, p50 1.33 ms, p95 1.66 ms,
5,585 bytes. Environment/load varies; no latency improvement is claimed.
The 74-byte payload increase carries explicit completeness metadata.

Deterministic layout + element preparation (50 runs, not browser rendering):
50 nodes p50 0.058 ms / p95 0.097 ms; 250 nodes p50 0.206 ms / p95 0.331 ms.
These are synthetic preparation costs, not production rendering or SQL accuracy.

Native Chrome screenshots of the isolated synthetic app confirmed accent-strip
cards, selected edge labels, collapsed options and populated inspector in light
and dark themes (2940x1602 physical capture, approximately 1470x801 CSS viewport).
The dark capture exposed tab/badge contrast issues, now corrected in CSS.
The exact 1440x900 / 1280x800 / 390x844 matrix remains NOT VERIFIED: native
focus protection interrupted viewport changes. Headless tests do not replace
responsive pixel verification. Screenshots are not committed. Live remote databases/providers and
production identity remain unverified/externally blocked.

Implementation: semantic_explorer.py, semantic_explorer_api.py,
web/semantic_explorer.js/.css and web/index.html.
Regression evidence: tests/test_semantic_explorer.py, test_debit_catalog.py and
semantic_explorer.cjs. Existing SQL-generation and conversation suites are retained.
