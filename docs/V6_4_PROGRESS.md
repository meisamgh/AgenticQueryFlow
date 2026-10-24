# V6.4 progress checkpoint

Baseline: `c2564d4` on `main`. Untracked `screenshots/` belongs to the user and is not part of V6.4 work.

Implemented in this checkpoint:

- Added provable SQL AST checks for direct GROUP BY dimensions, physical aggregate inputs, and required predicate operand/operator/value. Complex lineage stays `UNRESOLVED`; many-to-many aggregate joins surface a fanout warning rather than an unsupported rejection.
- Reconciled several contract edits: metric changes clear stale metric-dependent fields; conflicting equality filters are rejected; period and ranking updates preserve unrelated constraints.
- Passed the request's verified schema/profile snapshot into generation, avoiding a duplicate live inspection in the governed service path.
- Preserved a stable chart handle when Plotly rejects asynchronously and replaced the failed chart with ECharts. A DOM-mocked test rejects the actual `newPlot` promise.

Checks run: Python pytest full suite passed with one skipped test; 71 Deno frontend tests passed; Ruff, mypy over `src`, and `git diff --check` passed. No live-provider or PostgreSQL integration run was performed. These checks do not prove the entire V6.4 brief complete.

Still open: full semantic equivalence (derived formulas, temporal bounds, ranking, units, proven join fanout), complete delta/state semantics, plotting-source identity, conversational tool sequence consolidation, production identity/authorization, and the requested complete SQLite scenario matrix. See `V6_4_MATRIX.md` for individual statuses.

To resume: read this file and `V6_4_MATRIX.md`; inspect `git status`; complete the next high-severity NOT_STARTED or IN_PROGRESS row. Do not reimplement already VERIFIED behavior. Run `uv run pytest -q`, `uv run ruff check src tests`, `uv run mypy src`, `deno test --allow-read tests/frontend_rendering.cjs tests/voice_input_test.mjs`, and `git diff --check` after code changes. Record live-provider/PostgreSQL tests separately as NOT RUN when unavailable.
