# Semantic Explorer

Latest UI simplification: removed Refresh, Fit, Reset, zoom buttons, join-check
button and the visible advanced filter panel. Native zoom/pan/drag and bounded API
filter parameters remain; there is no competing execution workflow. Edges always
show connection types. Selecting a schema table expands its actual columns and
HAS_COLUMN links. Semantic Explorer navigation now has a 12px icon-label gap.
Rounded node cards use opaque pale semantic tints and dark-mode equivalents.
Unapproved metadata is labeled "Not reviewed" instead of the former wording;
approval facts and SQL grounding are unchanged. Real desktop Chrome verification
confirmed simplified controls, populated inspector, semantic cards and visible
USES_COLUMN/HAS_COLUMN labels on isolated synthetic metadata. Exact mobile/theme
viewport screenshots are still not claimed. Latest frontend checks: 224 passed;
Ruff/mypy/diff checks passed. Local .env and business databases were not changed.

## Compact graph refinement

KPI selection now automatically expands real input columns; expansion includes
their physical tables so HAS_COLUMN links retain both endpoints. FILTERED_BY
appears only when the catalog defines mandatory filters. Debit-card candidates
currently define none; no rule or KPI dependency is invented to populate the UI.
Opening KPI view preselects a KPI and fills the inspector. Expand/collapse and
inspection use only bounded metadata endpoints. Inspector metadata is expandable
and mobile users can toggle the panel.

The visual layout now packs source-connected components, compresses empty levels
and wraps larger levels. Rounded nodes use 14px labels; repetitive edge labels
appear on hover/selection. Selected nodes highlight their actual neighborhood.
Compact zoom icons, grouped Fit/Reset, concise status and cache-busted assets ship
with the existing theme. No SQL, model calls or source-graph mutations were added.

Latest checks: 950 Python passed / 2 PostgreSQL skipped; 223 frontend/voice passed;
Ruff, mypy and diff checks pass. Tests include 2/6/10/50/120-node synthetic layouts,
default populated inspector and real USES_COLUMN/HAS_COLUMN/FILTERED_BY expansion.
The exact viewport screenshot/light-dark matrix remains NOT RUN for this revision:
in-app browser is unavailable and native browser focus protection repeatedly
prevented actions. Do not treat headless Cytoscape checks as visual verification.


Open **Semantic Explorer** in the sidebar. Schema, KPI dependencies and Combined
views reuse the existing NetworkX snapshot, compiler, approved metric resolution
and join resolver. Cytoscape 3.30.4 (MIT) is vendored locally and loaded on demand.

All configured local datasources are exposed through the existing registry
allowlist. Unknown datasource IDs are rejected on every graph/detail route.
The loopback workspace restriction is unchanged; this is not remote tenant access.

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

## Workspace refinement (2026-11-08)

KPI Lineage defaults when KPI metadata exists; schema is the fallback. The workspace
uses neutral rounded cards with semantic border colors, a dotted canvas, compact
segmented navigation, zoom/fit/reset controls and a right-hand Node Inspector.
Source columns remain expandable. Combined opens around one metadata focus rather
than returning the whole network. Metadata search highlights matching nodes.
The source screenshot was available; the separately mentioned HTML file was not.

Business-rule approval derives from current authoritative owner metrics, not node
kind. Catalog-definition edges are dotted only for an authoritative owner; candidate
links stay suggested. Lineage labels distinguish approved, suggested, mixed and
unavailable evidence. No candidate is promoted into SQL grounding. Actual dependency
chains render when present; no artificial dependencies were added to debit metrics.
The original breadth-first KPI layout produced overlap in browser inspection;
deterministic visual layers now separate KPI dependencies, rules, columns and tables.
This is a frontend layout, not a new graph engine. Schema keeps breadth-first layout.

Checks: 950 Python tests passed, 2 PostgreSQL skipped; 221 frontend/voice tests
passed; Ruff, mypy and diff checks passed. Added authority-transition, candidate
rule, mixed lineage, default-view and deterministic layering regressions.
Latest synthetic metadata benchmark (100 warm TestClient requests): cold 12.52 ms,
p50 1.33 ms, p95 1.66 ms, 5,585 bytes, 8 nodes/11 edges. Different runs/load conditions
mean no speedup or slowdown claim is justified relative to earlier measurements.

Desktop browser verified workspace loading, KPI inspection and lineage interaction.
The exact 1440x900, 1280x800 and 390x844 light/dark visual matrix and final revised
layer layout screenshots remain NOT VERIFIED; browser-control focus interference
prevented completing that matrix. No browser screenshot is committed. Remote
identity/datasource authorization remains BLOCKED. No live-provider checks needed:
explorer fixtures forbid business execution and use zero model calls.
