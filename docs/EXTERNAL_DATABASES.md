# Database connections

SQLite/PostgreSQL and optional BigQuery, MySQL, SQL Server and Snowflake adapters
share the existing retrieval, grounding, SQL generation, validation, execution,
recovery and result-paging pipeline. No separate SQL-generation agent was added.

## Install drivers

```bash
uv sync --extra mysql --extra sqlserver --extra snowflake --extra bigquery --dev
```

SQL Server additionally needs the system ODBC driver manager and Microsoft's SQL
Server ODBC driver. Follow the driver's official installation/licensing procedure;
`pyodbc` alone is insufficient. BigQuery uses Google Application Default Credentials
(ADC). SQLAlchemy is pinned below 2.1 after Snowflake failed its import smoke test
against 2.1.6; version 2.0.55 passed.

## Connection settings

Set `TEXT2SQL_EXTERNAL_CONNECTIONS` locally to a JSON array, for example:

```json
[
  {"db_id":"warehouse","engine":"bigquery","url_env":"WAREHOUSE_URL","schema_name":"PROJECT.analytics","allowed_tables":["orders"],"read_only_role_confirmed":true,"maximum_bytes_billed":100000000},
  {"db_id":"sales","engine":"mysql","url_env":"SALES_URL","schema_name":"sales","allowed_tables":["orders"],"read_only_role_confirmed":true},
  {"db_id":"operations","engine":"sqlserver","url_env":"OPERATIONS_URL","schema_name":"dbo","allowed_tables":["orders"],"read_only_role_confirmed":true},
  {"db_id":"finance","engine":"snowflake","url_env":"FINANCE_URL","schema_name":"ANALYTICS","allowed_tables":["ORDERS"],"read_only_role_confirmed":true}
]
```

URL secrets belong only in the referenced local environment variables:

| Engine | URL prefix | SQLGlot dialect | Required driver/infrastructure |
| --- | --- | --- | --- |
| BigQuery | `bigquery://` | `bigquery` | Google credentials, project/dataset, query cost ceiling |
| MySQL | `mysql+pymysql://` | `mysql` | PyMySQL and a read-only database user |
| SQL Server | `mssql+pyodbc://` | `tsql` | pyodbc, system ODBC driver, read-only principal |
| Snowflake | `snowflake://` | `snowflake` | Snowflake connector/dialect, warehouse, read-only role |

These values are server-side settings. Never send credentials to the LLM, store them
in profile/glossary JSON, or commit them to Git. `read_only_role_confirmed` is only a
configuration acknowledgement, not evidence that server privileges were verified.

BigQuery's `schema_name` must be `project.dataset`; the common `url_env` field is
not used by its native ADC client. Set `TEXT2SQL_DEFAULT_DB_ID` to a configured ID
for the web application. Put the JSON array on one quoted line in `.env` and keep
connection secrets only in the referenced environment variables. Restart the app.
`configured=true` means registered, not verified connection health.

Generate the existing schema/column profile explicitly when needed (this runs
bounded read-only profiling queries, but no LLM calls):

```bash
uv run --env-file .env python -m semantic_text2sql.profile_connections --db-id sales --sample-limit 100
.venv/bin/uvicorn semantic_text2sql.api:app --host 127.0.0.1 --port 8001 --env-file .env
```

Text-encoded dates require an observed format in that profile; Queryflow must not
guess a date representation from a column name alone. BigQuery profiling still
obeys the configured byte-cost ceiling on every query.

Example URL shapes (placeholder credentials only; URL-encode passwords):

```ini
SALES_URL=mysql+pymysql://readonly:PASSWORD@HOST:3306/sales
OPERATIONS_URL=mssql+pyodbc://readonly:PASSWORD@HOST/operations?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes
FINANCE_URL=snowflake://readonly:PASSWORD@ACCOUNT/DATABASE/ANALYTICS?warehouse=WAREHOUSE&role=READONLY
```

## Execution and verification limits

- One parsed read-only query, schema/table/column allowlists, no arbitrary UDFs
  or table functions, parameterized value lookups and bounded row fetching.
- MySQL additionally uses a read-only transaction. SQL Server and Snowflake rely
  on provisioned SELECT-only roles as well as application checks. The configuration
  acknowledgement does not automatically verify server permissions.
- BigQuery dry-runs each query, checks the byte ceiling, applies a job timeout and
  attempts cancellation on failure. Dry-run and actual-job operations consume
  separate active request DB-budget units. LIMIT is not a cost ceiling.
- Query timeouts and small connection pools are bounded. Metadata caches expire
  after 60 seconds. Remote errors are sanitized to avoid credential disclosure.
- Per-engine EXPLAIN plans are not currently returned; no plan is fabricated and
  execution is not represented as an EXPLAIN.
- Nested/repeated BigQuery fields, unenforced key guarantees, arbitrary UDFs,
  cross-schema joins and table functions are intentionally not promoted as supported.

Scripted-driver integration tests cover all four engines through `/api/chat`,
schema inspection, dialect propagation, bounded execution, parameter binding,
forbidden SQL, scope checks, BigQuery cost rejection and timeout cancellation.
MySQL, Snowflake and BigQuery driver imports passed. On the development laptop,
SQL Server still lacks the system ODBC prerequisite. Live connections/provider SQL
accuracy are NOT RUN without credentials. These tests do not prove production
connectivity, business correctness or warehouse performance. Shared production
deployment additionally needs genuine user/tenant authorization.
