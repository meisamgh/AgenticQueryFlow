# Queryflow V6.4 implementation specification

Primary source: the user-provided 810-line brief dated 2026-10-24, titled “Queryflow V6.4 — Complete All Remaining Implementation Requirements” (last reviewed commit `c2564d4`). The numbered requirements and acceptance tests are tracked individually in [V6_4_MATRIX.md](V6_4_MATRIX.md).

Target ownership: the Top-layer LLM chooses analytical actions; a typed contract resolver preserves accepted semantics; the governed Text-to-SQL service retrieves verified schema and generates read-only SQL; deterministic safety and provable semantic checks gate execution; a dedicated plotting service uses the exact verified result; and the orchestrator commits accepted state and produces a grounded answer. Display-only chart mechanics may remain local.

Mandatory safeguards: no stale-result substitution, no unverified semantic claim, no SQL outside the read-only allowlist, no placeholder production authentication, and no claim of completion based solely on mocked tests. The full brief's sections 1–20 and its 32 integration scenarios remain the source of truth if this summary is ambiguous.
