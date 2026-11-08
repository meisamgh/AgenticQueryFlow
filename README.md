# AgenticQueryFlow

**From business questions to governed SQL, trusted data exploration, and interactive analytics.**

[AgenticQueryFlow](https://github.com/meisamgh/AgenticQueryFlow) is an open-source conversational analytics application for supported SQL data sources and analytical warehouses. Ask questions in natural language, inspect the generated read-only SQL and its results, refine analyses through follow-up conversations, and create charts or dashboards from validated data.

[**Architecture details**](docs/V6_4_PROGRESS.md) · [**Connection guide**](docs/EXTERNAL_DATABASES.md)

## Demo video

https://github.com/user-attachments/assets/cea65003-e31e-4846-a532-904ffcb6e25f

<p align="center">
  <img src="WorkFlow.png" alt="AgenticQueryFlow conversational analytics workflow" width="960" />
</p>

<details>
  <summary><strong>View detailed architecture</strong></summary>
  <p align="center">
    <img src="Arch-pic_ont.png" alt="Detailed semantic grounding and recovery architecture" width="960" />
  </p>
</details>

## What you can do

- Explore tables, relationships and approved KPI dependencies in the local **Semantic Explorer**. Search, expand neighborhoods, inspect upstream lineage and potential downstream impact without SQL execution or model calls.

- **Ask and refine questions.** A LangGraph assistant selects governed tools, maintains conversation context, and supports follow-ups, corrections, explanations, and investigations.
- **Generate grounded SQL.** Hybrid schema retrieval combines BM25, embeddings, value matching, and optional reranking with business glossary definitions and verified metadata.
- **Use governed business metrics.** An approved semantic catalog can ground formulas, dependencies, filters, and join relationships. NetworkX graph grounding is enabled by default and expands relevant relationships without another LLM call.
- **Use KPI-first grounding.** Enabled by default, it resolves approved aliases, pins metric dependencies before retrieval, and checks mandatory joins. [Configuration, example and measured limits](docs/SEMANTIC_GRAPH.md#kpi-first-grounding-default-on).
- **Inspect safe query execution.** SQLGlot checks, source/table allowlists, row limits, timeouts, and request budgets constrain generated queries.
- **Recover with evidence.** A bounded Recovery Agent investigates detected failures, empty results, unexpected NULLs, and explicit correctness reviews. An optional *local* ontology lookup provides approved semantic evidence; it is not an external ontology service.
- **Explore interactive charts.** Chart proposals are validated against complete accepted results. Plotly renders charts, with an ECharts fallback. Chart-only changes can reuse existing results without regenerating SQL.
- **Build dashboards conversationally.** Ask for a dashboard, refine its KPIs and visualizations, and save validated drafts in Dashboard Studio with version history and editing controls.
- **Inspect and revisit work.** View SQL, data, chart history, result versions, usage details, CSV exports, and optional voice transcription. SQL-based conversation memory can retrieve earlier accepted analytical definitions.

## How it works

```text
Business question
      |
      v
LangGraph assistant (intent, context, tool selection)
      |
      +-- Existing result? --------> Reuse accepted evidence
      |
      +-- New analysis? -----------> Hybrid retrieval
      |                                  |
      |                             Semantic grounding
      |                             (glossary, approved metrics,
      |                              NetworkX graph join resolver)
      |                                  |
      |                             SQL generation
      |                                  |
      |                             SQLGlot + semantic checks
      |                                  |
      |                             Bounded read-only execution
      |                                  |
      +------------------------------> Accepted result
                                           |
                             +-------------+-------------+
                             |                           |
                       Answer / Data              Chart / Dashboard
                             |                           |
                             +---- Inspect / refine -----+

On detected failures or explicit review:
Recovery Agent -> evidence tools -> REPAIR / INFORM / ESCALATE
```

The assistant never treats a successful SQL execution as proof that a business definition is correct. Chart and dashboard outputs must be grounded in actual accepted results; missing measures, filters, or dimensions may require another governed query.

## Architecture highlights

**One conversational orchestrator.** The Top-Layer LangGraph assistant delegates SQL generation, result inspection, charting, and dashboard operations to bounded tools. Specialized validation stays outside the LLM.

**Hybrid retrieval with graph grounding.** The existing retriever supplies schema candidates. A deterministic NetworkX layer adds permitted join paths and approved KPI dependencies from versioned metadata by default. Set `TEXT2SQL_GRAPH_ENABLED=false` to opt out. The semantic catalog—not the derived graph—is authoritative for approved metrics. Compatible JSON graph persistence is enabled by default.

**Evidence-based recovery.** The existing Recovery Agent uses schema inspection, value inspection, and bounded diagnostic queries. A feature-gated `query_ontology` tool can consult compatible local catalog/graph facts without an external ontology service.

**Result-aware visualization.** The assistant proposes chart encodings; the deterministic plotting service validates accepted columns, rows, completeness and result identity, using local recommendations when needed. No separate plotting-model fallback runs.

See [graph design and limitations](docs/SEMANTIC_GRAPH.md), [Phase 0 evaluation](docs/SEMANTIC_GRAPH_PHASE0.md), [dashboard workflow](docs/AUTONOMOUS_DASHBOARDS.md), and [Dashboard Studio](docs/DASHBOARD_STUDIO.md).

## Quick start

Requires **Python 3.12+** and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/meisamgh/AgenticQueryFlow.git
cd AgenticQueryFlow
uv sync --dev
cp .env.example .env
```

Configure a supported data source, its allowlist and your chosen model provider in `.env`. For a small local demo, you can generate the included sample data:

```bash
uv run python scripts/create_demo_db.py --root data
```

Select the resulting data-source ID using `TEXT2SQL_DEFAULT_DB_ID`, then start the application:

```bash
uv run uvicorn semantic_text2sql.api:app --host 127.0.0.1 --port 8001 --env-file .env
```

Open [http://127.0.0.1:8001](http://127.0.0.1:8001). Available API documentation is at [`/docs`](http://127.0.0.1:8001/docs).

For other supported connectors, optional drivers, access restrictions, and setup examples, see the [data-source connection guide](docs/EXTERNAL_DATABASES.md).

## Configuration

The [`.env.example`](.env.example) file documents all available settings. Common options:

| Setting | Purpose |
| --- | --- |
| `TEXT2SQL_DEFAULT_DB_ID` | Select the configured data source |
| `TEXT2SQL_EXTERNAL_CONNECTIONS` | Register additional supported connections |
| `TEXT2SQL_SQL_PROVIDER` / `TEXT2SQL_SQL_MODEL` | Configure the SQL-generation model |
| `TEXT2SQL_CONVERSATIONAL_AGENT_ENABLED` | Enable the conversational agent |
| `TEXT2SQL_CONTEXT_MODE` | Use retrieval or optional model-based context planning |
| `TEXT2SQL_GLOSSARY_ROOT` / `TEXT2SQL_SEMANTIC_CATALOG_ROOT` | Load business definitions and approved semantic facts |
| `TEXT2SQL_GRAPH_ENABLED` | NetworkX graph grounding (default: on; false opts out) |
| `TEXT2SQL_ONTOLOGY_RECOVERY_ENABLED` | Enable optional local ontology evidence during recovery (default: off) |
| `TEXT2SQL_DASHBOARD_LOCAL_WORKSPACE` | Configure the local dashboard workspace |

Keep credentials, local `.env` files, data files, and generated snapshots out of version control. Do not send secrets to models.

## Verification

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy src
deno test -A tests/*.cjs
git diff --check
```

These checks cover software behavior, including scripted integrations. They **do not** establish live connectivity, end-to-end business-semantic correctness, or production readiness. Graph-on accuracy and independent business-rule validation still require evaluation on the target workload.

## Project status and documentation

AgenticQueryFlow is intended for **controlled analytics pilots** with reviewed business definitions and governed access. Shared or public deployment requires production-grade identity, session ownership, authorization, and operational controls. External dashboard sharing is not enabled.

- [Implementation status and remaining requirements](docs/V6_4_PROGRESS.md)
- [V6.4 acceptance matrix](docs/V6_4_MATRIX.md)
- [Data-source setup](docs/EXTERNAL_DATABASES.md)
- [Offline semantic graph](docs/SEMANTIC_GRAPH.md)
- [Graph evaluation and known limitations](docs/SEMANTIC_GRAPH_PHASE0.md)
- [Dashboard architecture](docs/AUTONOMOUS_DASHBOARDS.md)
- [Dashboard Studio](docs/DASHBOARD_STUDIO.md)
- [Performance notes](docs/PERFORMANCE.md)
