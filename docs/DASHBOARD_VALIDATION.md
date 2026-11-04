# Dashboard validation and useful failures

## Confirmed root causes

The retained failed runs show:

1. `ValidationError`: `rationale` exceeded the existing 600-character limit.
2. `ValueError`: the retry hit the combined annotation rule, which rejected all
   digits, markup and certain finding verbs. That old log did not retain the
   offending field or distinguish which part of the combined rule matched.

The prompt now requests one concise sentence, preferably under 300 characters;
the schema's hard 600-character limit remains. There is no silent truncation of
the accepted proposal. Valid years/labels can be grounded in accepted temporal
observations, semantic periods, accepted filter/limit literals, columns and
validated chart labels. Arbitrary measure values are not annotation permissions.
Individual chart annotations are scoped to their own source, not another result.
Unsupported numerical claims, percentages/currency amounts, markup, bad IDs,
incorrect axes, source changes and invalid PATCH scopes still fail validation.

## Structured diagnostics

`DashboardDesignFailure` records code, field, safe message, repairability,
request ID, proposal-attempt ID, attempt number and exception type. The outcome
retains all failed attempts; chat and Studio error responses expose safe structured
diagnostics. A successful corrected design remains a successful result.

Common codes include `ANNOTATION_TOO_LONG`, `UNSUPPORTED_NUMERICAL_CLAIM`,
`ANNOTATION_MARKUP`, `INVALID_CANDIDATE`, `INVALID_AXIS`, `STALE_WIDGET`,
`MODE_MISMATCH`, `SOURCE_MISMATCH`, `INVALID_JSON`, `PROPOSAL_SCHEMA_ERROR`,
`REPAIR_SCOPE_VIOLATION`, `PROVIDER_REQUEST_REJECTED`, `PROVIDER_FAILURE`,
`DESIGN_BUDGET_EXHAUSTED`, `SOURCE_VALIDATION_FAILED` and `REVISION_CONFLICT`.

Logs contain opaque request/proposal IDs, attempt, code, field, repairability
and exception class, never raw proposals, SQL results, credentials or data values.
Unknown Pydantic field names are redacted to `unknown_field`. The client message
uses the safe cause and states whether an existing design was retained.

## Bounded repair

Only one design-repair call is allowed within the shared budget/deadline. It
receives the exact structured failure and a bounded, whitelisted snapshot of the
previous model proposal (not source rows or unknown extra fields). The snapshot
is untrusted data, not instructions. A deterministic scope check prevents an
explanation-only repair changing other selections. A reported invalid candidate
may be replaced, but unaffected valid selections are protected.

Provider HTTP errors, timeouts and budget errors are not fed through model-design
repair. A documented native-format capability rejection may use the existing
single schema fallback. Existing provider-level retry limits remain unchanged.
Failed repairs do not commit a dashboard revision or replace source snapshots.
Frontend errors prefer the current safe message over inherited explanation text.

## Verification

`test_dashboard_design_diagnostics.py` covers valid years, unsupported claims,
markup, unknown IDs, axes, stale replacements, mode confusion, precise field
diagnostics, safe logging, one successful repair, two failed attempts, repair scope,
provider non-retry and real SQLite/API preservation of the original revision.
Existing multi-source, authorization, saved-dashboard and native-provider tests
remain. Frontend tests cover current-cause display and successful repaired results.

Full checks: 678 Python passed, 2 skipped (22.58s); 189 frontend/voice passed
(509ms); Ruff, mypy (65 source files), `git diff --check` passed.
Providers are scripted for this verification; no new live-provider request,
PostgreSQL deployment test or browser pixel review was run. The running server,
local environment settings, credentials and model selections were left unchanged.

Changed files for this fix:

- `src/semantic_text2sql/ai_dashboard_designer.py`
- `src/semantic_text2sql/models.py`
- `src/semantic_text2sql/api.py`
- `src/semantic_text2sql/conversational_agent.py`
- `src/semantic_text2sql/dashboard_studio.py`
- `web/app.js`, `web/index.html`
- `tests/test_dashboard_design_diagnostics.py`, `tests/frontend_rendering.cjs`
- This document, `INTELLIGENT_DASHBOARD_DESIGN.md`, `V6_4_PROGRESS.md`, `V6_4_MATRIX.md`.

Earlier unrelated/uncommitted dashboard work was preserved. No commit or push.
