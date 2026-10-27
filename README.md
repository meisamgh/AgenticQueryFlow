# Queryflow — Semantic Text-to-SQL

Queryflow is a conversational analytics app for **SQLite** and allowlisted **PostgreSQL** databases. Ask questions in natural language, inspect the generated read-only SQL and actual results, refine an analysis through follow-up questions, or visualize an accepted result.

[Watch the demo](https://github.com/user-attachments/assets/99608fc4-5d31-419d-af3f-6cd446d09372)

<p align="center">
  <img src="Arch-pic" alt="Queryflow architecture" width="900" />
</p>

## Features

- **Conversational analytics:** a LangGraph assistant selects tools for new queries, refinements, explanations, investigations, and charts.
- **Grounded Text-to-SQL:** hybrid schema retrieval (BM25, embeddings, value matching), verified metadata, optional Model 1 context planning, and SQL generation.
- **Read-only execution:** SQLGlot validation, database allowlists, row limits, timeouts, and bounded model/database calls.
- **Evidence-based recovery:** investigate failed queries, empty results, unexpected NULLs, and explicit correctness-review requests before proposing a repair or escalation.
- **Validated charts:** a separate plotting service checks that complete accepted rows support the chart; Plotly is the primary renderer, with ECharts fallback.
- **Inspectable workspace:** SQL and Data tabs, CSV export, chart exploration, history, query versions, usage details, and optional voice transcription.

## How it works

```text
Natural-language request
        ↓
Top-layer assistant (intent and tool selection)
        ↓
Schema retrieval + semantic contract + business definitions
        ↓
Governed SQL generation → SQLGlot validation → read-only execution
        ↓
Verified rows ──→ optional bounded recovery
        ↓
Result reusability check → optional validated chart
        ↓
Grounded answer + SQL + data + conversation history
```

For chart-only changes, Queryflow can reuse a complete accepted result without regenerating SQL. Changes to filters, measures, periods, grouping, or other analytical requirements may need a new governed query. Models cannot directly execute arbitrary SQL or invent missing chart data.

## Quick start

Requires **Python 3.12+** and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/meisamgh/semantic_text2sql_ideal.git
cd semantic_text2sql_ideal
uv sync --dev
cp .env.example .env
```

Add your provider API key to `.env`. The example configuration uses Groq; the available model must be supported by your provider account.

Configure a database before running a question. For SQLite, place a database at `data/<db_id>/<db_id>.sqlite` (for example, `data/books/books.sqlite`) and set `TEXT2SQL_DEFAULT_DB_ID` accordingly. Generate an optional offline profile:

```bash
uv run python scripts/profile_database.py --dialect sqlite --db-id books --output profiles
```

Launch the application:

```bash
uv run uvicorn semantic_text2sql.api:app --host 127.0.0.1 --port 8000 --env-file .env
```

Open **http://127.0.0.1:8000**. Health check: `GET /api/health`.

For PostgreSQL, configure an allowlist with read-only credentials:

```dotenv
TEXT2SQL_POSTGRES_DATABASES={"analytics":"postgresql://reader:password@localhost/analytics"}
TEXT2SQL_DEFAULT_DB_ID=analytics
```

## Configuration

The public [`.env.example`](.env.example) provides a minimal single-model setup. Key options:

| Setting | Purpose |
| --- | --- |
| `TEXT2SQL_SQL_PROVIDER`, `TEXT2SQL_SQL_MODEL` | SQL model; also the default for assistant and plotting |
| `TEXT2SQL_CONVERSATIONAL_AGENT_ENABLED` | Enable the Top-layer conversational assistant |
| `TEXT2SQL_CONTEXT_MODE` | `retrieval` (default example) or `model1` |
| `TEXT2SQL_DEFAULT_DB_ID` | Selected allowlisted database |
| `TEXT2SQL_DATABASE_ROOT` | SQLite database directory |
| `TEXT2SQL_POSTGRES_DATABASES` | PostgreSQL allowlist |
| `TEXT2SQL_CONVERSATION_STORE` | Optional durable conversation state |
| `TEXT2SQL_RUN_HISTORY_PATH` | Saved conversation-turn history |

Assistant, context, and plotting models can be configured independently when needed. Keep `.env`, database files, and credentials out of version control. Optional LightGBM reranking is installed with `uv sync --dev --extra ml`.

## API

Use `POST /api/chat` for a synchronous request, or `POST /api/chat/jobs` for a cancellable job with progress polling.

```bash
curl -X POST http://127.0.0.1:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"demo-1","db_id":"books","message":"Count books by category."}'
```

Useful endpoints: `GET /api/health`, `GET /api/databases`, `GET /api/chat/jobs/{job_id}`, and `DELETE /api/chat/jobs/{job_id}`. FastAPI's API schema is available at `/docs` when the server is running.

## Verification

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy src
node --test tests/frontend_rendering.cjs
```

These checks validate software behavior, **not business-semantic correctness**. SQL that executes successfully can still encode an incorrect analytical definition. Full formula, temporal, ranking, and join-grain equivalence are not proven; live-provider and PostgreSQL integrations require separate verification.

## Documentation and status

- [V6.4 requirements and acceptance matrix](docs/V6_4_MATRIX.md)
- [V6.4 implementation specification](docs/V6_4_SPEC.md)
- [Implementation progress and known gaps](docs/V6_4_PROGRESS.md)
- [Performance notes](docs/PERFORMANCE.md)
- [Conversational assistant details](docs/V6_2_CONVERSATIONAL_ASSISTANT.md)

**Scope:** Queryflow is intended for controlled analytics pilots with governed access and reviewed business definitions. It is not an unrestricted or production-hardened autonomous SQL agent; production identity, authorization, and remaining V6.4 requirements still need work.
