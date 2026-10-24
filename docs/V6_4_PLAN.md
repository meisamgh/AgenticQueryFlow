# V6.4 dependency order

1. Semantic contract consistency and SQL semantic validation.
2. Request-local turn context, result identity, and commit boundaries.
3. Bounded SQL/plot/investigation orchestration and actionable requery.
4. Historical restore, reset, cancellation, and concurrency.
5. Router, schema snapshot, and context-path consolidation.
6. Approved metric discovery and one plotting owner.
7. Provider budgets, failure categories, and telemetry.
8. Plotly/frontend state synchronization.
9. Security and deployment boundaries.
10. Integration verification and independent audit.

After each code change: add a positive and negative regression test, run targeted tests, then the full relevant checks. Update the matrix only with observed evidence. Keep unsupported integration tests explicitly NOT RUN.
