# Governed semantic catalogs

A directory of versioned, human-governed semantic catalog documents, one per
database: `semantic_catalogs/<db_id>.json`. The loader is `semantic_catalog.SemanticCatalogStore`
(root configurable with `TEXT2SQL_SEMANTIC_CATALOG_ROOT`, default `data/semantic_catalogs`).

This folder is part of the blueprint's Phase A/B delivery (collaborative Text-to-Tableau
agent, end-to-end architecture v2). Complementary reading: the main project README
section "Semantic catalog" and the blueprint repository-informed update
(`tableau_collaborative_agent_e2e_v2.md` in the dashboard project).

## Status model

Every catalog fact (entity, grain, join, metric) carries exactly one status:

| Status | Meaning | Who can mint it |
|---|---|---|
| `observed` | recorded deterministic observation | probes / composition (never promotes other facts) |
| `inferred` | machine-authored candidate awaiting review | anything (the default for generated facts) |
| `reviewed` | human looked at it, not yet contract-grade | a human editing this JSON |
| `approved` | authoritative business contract | **only** a human `approval` record |
| `stale` | was stable, live database drifted | composition (computed, not stored) |
| `conflicting` | evidence disproves the declared fact | verification probes (computed, not stored) |

Structural rules enforced by Pydantic (no exceptions):

- `approved` ⇔ an `approval` record exists with `approval_source: "human_review"`.
  LLM output and heuristic confidence can never mint one.
- Machine-authored facts land as `inferred` and stay there. A passing probe never
  promotes, and live drift never demotes, anything that is not `approved`/`reviewed`/`observed`.
- Metrics must reference declared entities, and their `required_fields` must be
  fields the entity's quality contract asserts something about.
- Join sides must declare the same number of key columns.

## Document shape (worked example)

See [`market_data.json`](market_data.json) — the governed catalog for the
`market_data` PostgreSQL database used by the dashboard project:

- One entity, `market_index_daily` (`index_prices`), with a grain
  `(symbol, trade_date)` that is **approved** (primary key observed live, human
  approval record held by the data owner).
- Six business metrics (`close_level`, `daily_return`, `cumulative_return`,
  `normalized_level`, `annualized_volatility`, `max_drawdown`), all `inferred`
  and awaiting human review. Non-additive measures are flagged; monthly rollups
  use end-of-period observations, never averaged closes.

## Verification probes

`GET /api/databases/{db_id}/semantic-catalog/verification` runs read-only probes
against the live database and returns evidence-backed claims:

| Probe | Checks |
|---|---|
| `relation_exists` | entity relation executes |
| `columns_match` | all declared columns are actually live |
| `grain_unique` | duplicate grain rows (`GROUP BY ... HAVING COUNT(*) > 1`) |
| `required_not_null` | quality contract `required` columns |
| `positive_measure` | quality contract `positive` columns are non-null and > 0 |
| `date_coverage` | time column has observations (exact min/max returned) |

Combining rules: a `fail` turns stable facts (`approved`/`reviewed`/`observed`)
into `conflicting`; a missing relation turns them into `stale`; a skipped probe
(connection/SQL error) leaves the declared status untouched. Every claim cites
its probe evidence; the report also recomputes the schema fingerprint (a mismatch
makes the whole document `stale`).

`GET /api/databases/{db_id}/semantic-catalog` composes the document with live
schema status without running probes.

## How to govern your own database

1. Copy the structure of `market_data.json`; declare entities with real relation
   and column identifiers (must be probed-true, not aspirational).
2. Record grain and quality contracts; mark `inferred` anything you have not
   personally verified.
3. Add metrics referencing those entities. Leave them `inferred` until reviewed.
4. To approve a fact by hand: set `status: "approved"` and attach the approval
   record (`approved_by`, `approved_at`, `approval_source: "human_review"`,
   `evidence`). The model rejects approval without the record.
5. Store a `schema_fingerprint` (from `/verification`'s
   `live_schema_fingerprint`) to detect drift on every read.
6. Re-run `/verification` after schema or data changes; resolve every
   `conflicting` claim before trusting the catalog.

Constraints: JSON sidecar files only (no YAML dependency); files are re-read on
mtime change; nothing here executes SQL other than the verification probes,
which run through the same read-only registry connections as the rest of the API.
