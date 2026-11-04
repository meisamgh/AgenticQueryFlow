# AgenticQueryflow

[AgenticQueryflow](https://github.com/meisamgh/AgenticQueryFlow) is a conversational analytics application for **SQLite** and allowlisted **PostgreSQL** databases.

It transforms natural-language business questions into governed SQL, inspectable results, and interactive charts. Users can refine analyses through follow-up questions, inspect generated SQL, and explore validated results.

[Watch the demo](https://github.com/user-attachments/assets/cea65003-e31e-4846-a532-904ffcb6e25f)

<p align="center">
  <img src="Arch-pic.png" alt="AgenticQueryflow architecture" width="900" />

  <img src="WorkFlow.png" alt="Queryflow architecture" width="900" />
</p>

<p align="center">
  <img src="Arch-pic_ont.png" alt="Queryflow architecture" width="900" />
</p>



## Features

- **Conversational analytics:** A LangGraph assistant selects tools for new queries, refinements, explanations, investigations, and charts.
- **Grounded Text-to-SQL:** Hybrid schema retrieval using BM25, embeddings, value matching, verified metadata, optional Model 1 context planning, and SQL generation.
- **Read-only execution:** SQLGlot validation, database allowlists, row limits, timeouts, and bounded model/database calls.
- **Evidence-based recovery:** Investigates failed queries, empty results, unexpected NULLs, and explicit correctness-review requests before proposing repair or escalation.
- **Validated charts:** A separate plotting service validates accepted results before visualization, using Plotly with an ECharts fallback.
- **Inspectable workspace:** SQL and Data tabs, CSV export, chart exploration, query history, versions, usage details, and optional voice transcription.


## How it works

```text
Conversation → Top-layer assistant → Selected tools
                                      ├─ Reuse accepted results / inspect history
                                      ├─ Retrieve schema → Ground context → Generate SQL
                                      │                   → Validate → Read-only execution
                                      └─ Validated chart planning over accepted data

Failures, empty results, unexpected NULLs or correctness reviews
    → Bounded recovery → Gather evidence → Repair or explain the limitation
```

- **Conversational continuity:** accepted contracts, stable result IDs and scoped history.
  [SQL-based memory](docs/SQL_MEMORY.md) retrieves analytical definitions on demand;
  refreshed data is distinguished from original historical snapshots.
- **Compact context:** hybrid retrieval with optional model-based context selection.
  Optional [offline graph grounding](docs/SEMANTIC_GRAPH.md) adds approved KPI
  dependencies and bounded, provenance-aware join paths without extra LLM calls.
- **Governed execution:** SQLGlot checks, allowlisted data access and bounded repair.
- **Interactive results:** Visualization, Data and SQL views; charts reuse sufficient results.
- **Traceability:** tool observations, validation attempts, timings and token accounting.

**Conversational dashboards:** ask “Build a sales performance dashboard.” The
existing Top-layer agent reuses accepted evidence, retrieves missing analyses
through governed SQL, and invokes the bounded dashboard designer. Widgets keep
their own source IDs, scopes and grain. Validated designs render immediately,
without a proposal-confirmation step, creation button or mode selection.
Select a component and choose **Edit with AI**, or describe a whole-dashboard change.
Changes update the validated preview directly; Save and Publish remain separate actions.
[Autonomous workflow](docs/AUTONOMOUS_DASHBOARDS.md) · [Limits](docs/AGENTIC_DASHBOARDS.md).

Design follows verified **filters → KPIs → complementary plots**, with bounded
statistical evidence and source-preserving conversational edits.
[Designer details](docs/INTELLIGENT_DASHBOARD_DESIGN.md).

**Dashboard Studio:** save private drafts, collaborate on design, explicitly
approve versions, and reopen saved snapshots from the Library after restart—no
SQL rerun or model call. Includes version history, undo and conflict-safe saves.
External sharing remains disabled. [Setup and security](docs/DASHBOARD_STUDIO.md).

## Run locally

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/meisamgh/agenticqueryflow.git
cd agenticqueryflow
uv sync --dev
cp .env.example .env
# Configure your database and model credentials in .env.
.venv/bin/uvicorn semantic_text2sql.api:app --host 127.0.0.1 --port 8001 --env-file .env
```

Open **http://127.0.0.1:8001**. For a small local dataset:

```bash
uv run python scripts/create_demo_db.py --root data
```

Then set `TEXT2SQL_DEFAULT_DB_ID=books` in `.env` and restart.

## Settings

| Setting | Purpose |
| --- | --- |
| `TEXT2SQL_DATABASE_ROOT`, `TEXT2SQL_DEFAULT_DB_ID` | SQLite directory and selected database |
| `TEXT2SQL_POSTGRES_DATABASES` | PostgreSQL connection allowlist |
| `TEXT2SQL_EXTERNAL_CONNECTIONS` | Optional remote database definitions |
| `TEXT2SQL_PROFILE_ROOT` | Stored schema and column profiles |
| `TEXT2SQL_GLOSSARY_ROOT` | Approved business glossary JSON files |
| `TEXT2SQL_CONTEXT_MODE` | `retrieval` or optional `model1` selection |
| `TEXT2SQL_ASSISTANT_*`, `TEXT2SQL_SQL_*`, `TEXT2SQL_PLOTTING_*` | Model/provider selection by task |

Set the selected adapter's API key and base URL using [.env.example](.env.example).
Never commit `.env` or credentials. Profiles and glossary files supplement—not
replace—the database connection.

**Databases:** SQLite, PostgreSQL, plus optional MySQL, SQL Server, Snowflake and
BigQuery adapters. See [connection setup](docs/EXTERNAL_DATABASES.md) for driver
extras, read-only permissions, BigQuery cost ceilings and profiling. SQL Server
also requires a system ODBC driver.

## Verification and scope

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy src
deno test -A tests/*.cjs
git diff --check
```

Tests cover real SQLite execution and scripted remote-driver integration.
Live remote connectivity requires credentials and infrastructure; scripted tests
do not establish live availability or business-semantic correctness. Successful
SQL execution alone does not prove the answer is correct. Shared production
deployment additionally requires user/tenant authorization and operational review.

[Architecture and progress](docs/V6_4_PROGRESS.md) ·
[Database setup](docs/EXTERNAL_DATABASES.md) ·
[Performance measurements](docs/PERFORMANCE.md)
