"""FastAPI application for interactive generation and SQL checking."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, perf_counter
from typing import Any, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from semantic_text2sql.agent import TextToSQLAgent
from semantic_text2sql.analytics_planner import (
    PlannerDecisionRecord,
    metrics_ledger,
    plan_analytics,
    plan_fields_known,
    plan_needs_refinement,
    planner_enabled,
    planner_llm_enabled,
    refine_plan_with_model,
)
from semantic_text2sql.conversation import (
    ANALYTICAL_CORRECTION_OPERATIONS,
    EXPLANATION_OPERATIONS,
    STATE_REQUIRED_OPERATIONS,
    ConversationConflict,
    ConversationStore,
    classify_operation,
    intent_target,
    interpret_turn_detailed,
    requires_model_interpretation,
    resolve_turn,
)
from semantic_text2sql.data_requirement_validator import validate_reusability
from semantic_text2sql.database import DatabaseError, DatabaseRegistry
from semantic_text2sql.explanation import deterministic_explanation, explain_turn_detailed
from semantic_text2sql.filter_discovery import (
    _SENSITIVE_TYPES as _FILTER_FORBIDDEN_SEMANTIC_TYPES,
)
from semantic_text2sql.filter_discovery import (
    _UNSAFE_NAME as _FORBIDDEN_NAME_PATTERN,
)
from semantic_text2sql.filter_discovery import discover_filters
from semantic_text2sql.glossary import GlossaryStore, GlossaryTerm
from semantic_text2sql.historical import HistoricalQueryStore
from semantic_text2sql.hybrid_retrieval import (
    FastEmbedEncoder,
    HybridSchemaRetriever,
    LightGBMSchemaReranker,
)
from semantic_text2sql.llm import (
    AgentRouterClaudeModel,
    AgentRouterCodexModel,
    AgentRouterModel,
    CodeCraftSQLModel,
    GroqSQLModel,
    JustDoWorkSQLModel,
    ModelError,
    RoutingSQLModel,
)
from semantic_text2sql.models import (
    DEFAULT_MODEL,
    Attempt,
    CallLedgerEntry,
    ChatRequest,
    ChatResponse,
    CheckRequest,
    CheckResponse,
    ConversationState,
    DatabaseOption,
    FilterValuesResponse,
    GenerateResponse,
    HumanReviewRequest,
    ModelOption,
    ModelProvider,
    PresentationState,
    ResultPresentation,
    TokenUsage,
    TurnInterpretation,
    ValidationResult,
)
from semantic_text2sql.postgres import PostgresRegistry, postgres_databases_from_environment
from semantic_text2sql.presentation import present_result
from semantic_text2sql.profiling import ProfileStore
from semantic_text2sql.provider_registry import configured_model_options, supports_model
from semantic_text2sql.recovery_events import recovery_events
from semantic_text2sql.run_history import RunHistoryStore, RunRecord, build_run_record
from semantic_text2sql.runtime import RequestBudget, RequestBudgetExceeded
from semantic_text2sql.semantic_catalog import SemanticCatalogStore, compose_document
from semantic_text2sql.semantic_catalog_validation import (
    postgres_runner,
    sqlite_runner,
    verify_catalog,
)
from semantic_text2sql.service import TextToSQLService
from semantic_text2sql.sql_diffs import (
    SqlDiffRequest,
    SqlDiffResponse,
    sql_changelog,
)
from semantic_text2sql.strategy import route_question
from semantic_text2sql.visualization import (
    VisualizationError,
    parse_intent,
    plan_chart,
    sql_output_requirement,
)
from semantic_text2sql.visualization_recommender import (
    materialize_recommendation,
    merge_recommendations,
    recommend_charts,
    refine_charts_with_model,
    viz_recommender_llm_enabled,
)
from semantic_text2sql.voice import (
    MAX_AUDIO_BYTES,
    MAX_AUDIO_SECONDS,
    GroqVoiceTranscriber,
    QwenVoiceTranscriber,
    TranscriptionResult,
    VoiceInputError,
    VoiceProviderError,
)

logger = logging.getLogger(__name__)


def _planner_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def create_app(
    agent: TextToSQLAgent | None = None,
    conversation_completers: dict[str, Any] | None = None,
    voice_transcriber: QwenVoiceTranscriber | GroqVoiceTranscriber | None = None,
    run_history: RunHistoryStore | None = None,
) -> FastAPI:
    enforce_model_catalog = agent is None
    if voice_transcriber is not None:
        voice = voice_transcriber
    else:
        qwen_voice = QwenVoiceTranscriber.from_environment()
        voice = (
            qwen_voice
            if qwen_voice.unavailable_reason() is None
            else GroqVoiceTranscriber.from_environment()
        )
    runs = run_history or RunHistoryStore.from_environment()
    postgres_databases = postgres_databases_from_environment()
    postgres = PostgresRegistry(postgres_databases) if postgres_databases else None
    sqlite = DatabaseRegistry(Path(os.environ.get("TEXT2SQL_DATABASE_ROOT", "data")))
    profiles = ProfileStore(Path(os.environ.get("TEXT2SQL_PROFILE_ROOT", "profiles")))
    glossaries = GlossaryStore(
        Path(os.environ.get("TEXT2SQL_GLOSSARY_ROOT", "data/business_glossaries"))
    )
    semantic_catalogs = SemanticCatalogStore(
        Path(os.environ.get("TEXT2SQL_SEMANTIC_CATALOG_ROOT", "data/semantic_catalogs"))
    )
    history = HistoricalQueryStore(
        Path(
            os.environ.get("TEXT2SQL_HISTORY_PATH", "benchmarks/data/bird_history_seed42_400.json")
        )
    )
    agentrouter_key = os.environ.get("AGENTROUTER_API_KEY")
    agentrouter_base_url = os.environ.get("AGENTROUTER_BASE_URL", "https://agentrouter.org")
    agentrouter_timeout = float(os.environ.get("AGENTROUTER_TIMEOUT_SECONDS", "180"))
    agentrouter = AgentRouterModel(
        AgentRouterClaudeModel(
            agentrouter_key,
            agentrouter_base_url,
            agentrouter_timeout,
        ),
        AgentRouterCodexModel(
            agentrouter_key,
            agentrouter_base_url,
            agentrouter_timeout,
        ),
    )
    groq = GroqSQLModel(
        os.environ.get("GROQ_API_KEY"),
        os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
    )
    justdowork = JustDoWorkSQLModel(
        os.environ.get("JUSTDOWORK_API_KEY"),
        os.environ.get("JUSTDOWORK_BASE_URL"),
        float(os.environ.get("JUSTDOWORK_TIMEOUT_SECONDS", "120")),
    )
    codecraft = CodeCraftSQLModel(
        os.environ.get("CODECRAFT_API_KEY"),
        os.environ.get("CODECRAFT_BASE_URL", "https://codecraftapi.com/v1"),
        float(os.environ.get("CODECRAFT_TIMEOUT_SECONDS", "120")),
    )
    active_agent = agent or TextToSQLAgent(
        sqlite,
        RoutingSQLModel(agentrouter, groq, justdowork, codecraft),
        postgres,
        profiles,
    )
    turn_completers = conversation_completers or {
        "agentrouter": agentrouter,
        "groq": groq,
        "justdowork": justdowork,
        "codecraft": codecraft,
    }
    app = FastAPI(
        title="Semantic Text-to-SQL v5",
        version="0.5.0",
        description=(
            "Bounded context selection, compact grounding, SQL-only generation, "
            "safety-first validation, and read-only execution."
        ),
    )
    web_root = Path(__file__).resolve().parents[2] / "web"
    if web_root.is_dir():
        app.mount("/static", StaticFiles(directory=web_root), name="static")
    reranker = None
    reranker_path = os.environ.get("TEXT2SQL_SCHEMA_RERANKER_MODEL")
    if os.environ.get("TEXT2SQL_SCHEMA_RERANKER_ENABLED", "false").casefold() == "true":
        try:
            reranker = LightGBMSchemaReranker(Path(reranker_path or ""))
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            logger.warning("schema_reranker_disabled=%s", exc)
    hybrid_retriever = HybridSchemaRetriever(
        FastEmbedEncoder(os.environ.get("TEXT2SQL_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")),
        max_tables=int(os.environ.get("TEXT2SQL_RETRIEVAL_TABLES", "5")),
        max_columns_per_table=int(os.environ.get("TEXT2SQL_RETRIEVAL_COLUMNS", "5")),
        reranker=reranker,
        reranker_pool=int(os.environ.get("TEXT2SQL_SCHEMA_RERANKER_POOL", "30")),
    )
    question_service = TextToSQLService(
        database=sqlite,
        postgres=postgres,
        profiles=profiles,
        glossaries=glossaries,
        history=history,
        retriever=hybrid_retriever,
        agent=active_agent,
        context_completers=cast(dict[ModelProvider, Any], turn_completers),
    )
    conversation_store_value = os.environ.get("TEXT2SQL_CONVERSATION_STORE")
    conversation_path = Path(conversation_store_value) if conversation_store_value else None
    conversations = ConversationStore(
        conversation_path,
        ttl_seconds=int(os.environ.get("TEXT2SQL_CONVERSATION_TTL_SECONDS", "86400")),
    )
    # Complete bounded results are kept only for the active process. Persisted
    # history retains a smaller preview and is reused only when it is complete.
    recent_results: dict[tuple[str, str], ChatResponse] = {}
    chat_jobs: dict[str, dict[str, Any]] = {}
    job_ttl_seconds = max(60, int(os.environ.get("TEXT2SQL_JOB_TTL_SECONDS", "3600")))
    max_jobs = max(10, int(os.environ.get("TEXT2SQL_MAX_JOBS", "500")))

    def cleanup_jobs() -> None:
        now = perf_counter()
        expired = [
            job_id
            for job_id, job in chat_jobs.items()
            if job.get("status") in {"completed", "failed", "cancelled"}
            and now - float(job.get("finished_at") or job["started_at"]) > job_ttl_seconds
        ]
        for job_id in expired:
            chat_jobs.pop(job_id, None)
            chat_tasks.pop(job_id, None)
        if len(chat_jobs) > max_jobs:
            terminal = sorted(
                (
                    (job_id, float(job.get("finished_at") or job["started_at"]))
                    for job_id, job in chat_jobs.items()
                    if job.get("status") in {"completed", "failed", "cancelled"}
                ),
                key=lambda item: item[1],
            )
            for job_id, _ in terminal[: len(chat_jobs) - max_jobs]:
                chat_jobs.pop(job_id, None)
                chat_tasks.pop(job_id, None)

    def set_session_stage(session_id: str, stage: str) -> None:
        for job in chat_jobs.values():
            if job.get("session_id") == session_id and job.get("status") == "running":
                if job.get("stage") != stage:
                    job.setdefault("events", []).append(
                        {
                            "stage": stage,
                            "elapsed_ms": round((perf_counter() - float(job["started_at"])) * 1000),
                        }
                    )
                job["stage"] = stage

    chat_tasks: dict[str, asyncio.Task[None]] = {}

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "online"}

    @app.get("/", include_in_schema=False)
    async def web_application() -> FileResponse:
        return FileResponse(web_root / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/models", response_model=list[ModelOption])
    async def models(response: Response) -> list[ModelOption]:
        response.headers["Cache-Control"] = "no-store"
        return configured_model_options()

    @app.get("/api/ui-config")
    async def ui_config(response: Response) -> dict[str, str | None]:
        response.headers["Cache-Control"] = "no-store"
        return {
            "sql_provider": os.environ.get("TEXT2SQL_SQL_PROVIDER") or "agentrouter",
            "sql_model": os.environ.get("TEXT2SQL_SQL_MODEL") or DEFAULT_MODEL,
            "context_mode": os.environ.get("TEXT2SQL_CONTEXT_MODE") or "retrieval",
            "context_provider": os.environ.get("TEXT2SQL_CONTEXT_PROVIDER") or None,
            "context_model": os.environ.get("TEXT2SQL_CONTEXT_MODEL") or None,
            "default_db_id": os.environ.get("TEXT2SQL_DEFAULT_DB_ID") or "debit_card_specializing",
        }

    @app.get("/api/voice")
    async def voice_configuration(response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        reason = voice.unavailable_reason()
        return {
            "configured": reason is None,
            "provider": voice.provider,
            "model": voice.model,
            "max_duration_seconds": MAX_AUDIO_SECONDS,
            "unavailable_reason": reason,
        }

    @app.post("/api/voice/transcribe", response_model=TranscriptionResult)
    async def transcribe_voice(request: Request, response: Response) -> TranscriptionResult:
        response.headers["Cache-Control"] = "no-store"
        if reason := voice.unavailable_reason():
            raise HTTPException(status_code=503, detail=reason)
        if request.headers.get("content-type", "").split(";")[0].strip() != "audio/wav":
            raise HTTPException(status_code=415, detail="Send a WAV recording from the microphone.")
        audio = bytearray()
        async for chunk in request.stream():
            if len(audio) + len(chunk) > MAX_AUDIO_BYTES:
                raise HTTPException(status_code=413, detail="Record up to 60 seconds of audio.")
            audio.extend(chunk)
        try:
            return await voice.transcribe(bytes(audio))
        except VoiceInputError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except VoiceProviderError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.get("/api/history")
    async def history_list(response: Response, session_id: str, limit: int = 50) -> list[Any]:
        response.headers["Cache-Control"] = "no-store"
        return [
            item.model_dump(exclude={"response_payload", "state_snapshot"})
            for item in runs.list_sessions(session_id, limit=min(limit, 200))
        ]

    @app.get("/api/history/{run_id}")
    async def history_run(response: Response, run_id: str, session_id: str) -> RunRecord:
        response.headers["Cache-Control"] = "no-store"
        record = runs.get(run_id, session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Run not found.")
        return record

    @app.get("/api/history/{run_id}/turns", response_model=list[RunRecord])
    async def history_turns(response: Response, run_id: str, session_id: str) -> list[RunRecord]:
        response.headers["Cache-Control"] = "no-store"
        if runs.get(run_id, session_id) is None:
            raise HTTPException(status_code=404, detail="Conversation turn not found.")
        return runs.lineage(session_id, run_id)

    @app.post("/api/history/{run_id}/continue", response_model=ConversationState)
    async def continue_history(run_id: str, session_id: str) -> ConversationState:
        record = runs.get(run_id, session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Conversation turn not found.")
        if record.state_snapshot is None:
            raise HTTPException(status_code=409, detail="This older turn has no restorable state.")
        current = conversations.get(session_id)
        try:
            return conversations.put(
                record.state_snapshot,
                expected_version=current.version if current else None,
            )
        except ConversationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/sql/diff", response_model=SqlDiffResponse)
    async def sql_diff(payload: SqlDiffRequest, response: Response) -> SqlDiffResponse:
        response.headers["Cache-Control"] = "no-store"
        dialect = "postgres" if payload.dialect == "postgres" else "sqlite"
        return sql_changelog(payload.previous_sql, payload.updated_sql, dialect=dialect)

    @app.delete("/api/history", status_code=204)
    async def history_clear(response: Response, session_id: str) -> Response:
        response.headers["Cache-Control"] = "no-store"
        runs.drop_session(session_id)
        return Response(status_code=204)

    @app.get("/api/databases/{db_id}/glossary")
    async def database_glossary(response: Response, db_id: str) -> list[GlossaryTerm]:
        """Approved business definitions, for the contextual schema inspector."""
        response.headers["Cache-Control"] = "no-store"
        glossary = glossaries.load(db_id)
        return glossary.terms if glossary else []

    @app.get("/api/databases", response_model=list[DatabaseOption])
    async def databases() -> list[DatabaseOption]:
        options = [
            DatabaseOption(db_id=db_id, dialect="sqlite", configured=True)
            for db_id in sqlite.list_ids()
        ]
        if postgres is not None:
            options.extend(
                DatabaseOption(db_id=db_id, dialect="postgres", configured=True)
                for db_id in postgres.configured_ids()
            )
        elif not postgres_databases:
            options.append(
                DatabaseOption(db_id="books_postgres", dialect="postgres", configured=False)
            )
        return options

    @app.get("/api/databases/{db_id}/schema")
    async def database_schema(db_id: str) -> dict[str, Any]:
        """Browse live schema and labeled profile relationships without exposing rows."""
        if db_id in sqlite.list_ids():
            schema = await asyncio.to_thread(sqlite.inspect, db_id)
        elif postgres is not None and db_id in postgres.configured_ids():
            schema = await asyncio.to_thread(postgres.inspect, db_id)
        else:
            raise HTTPException(status_code=404, detail="Database was not found or configured.")
        live_columns = {
            (table.name, column.name) for table in schema.tables for column in table.columns
        }
        relationships = [
            {
                "from_table": item.from_table,
                "from_column": item.from_column,
                "to_table": item.to_table,
                "to_column": item.to_column,
                "source": "declared_foreign_key",
                "cardinality": None,
            }
            for item in schema.relationships
        ]
        declared = {
            (item.from_table, item.from_column, item.to_table, item.to_column)
            for item in schema.relationships
        }
        try:
            profile = profiles.load(schema.dialect, db_id)
        except (OSError, ValueError):
            profile = None
            logger.warning("Could not read schema profile for %s", db_id, exc_info=True)
        if profile is not None:
            for item in profile.relationships:
                key = (
                    item.child_table,
                    item.child_column,
                    item.parent_table,
                    item.parent_column,
                )
                if key in declared or any(
                    part not in live_columns
                    for part in (
                        (item.child_table, item.child_column),
                        (item.parent_table, item.parent_column),
                    )
                ):
                    continue
                relationships.append(
                    {
                        "from_table": item.child_table,
                        "from_column": item.child_column,
                        "to_table": item.parent_table,
                        "to_column": item.parent_column,
                        "source": "profile_inference",
                        "cardinality": item.type,
                    }
                )
        return {
            "db_id": db_id,
            "dialect": schema.dialect,
            "tables": [
                {
                    "name": table.name,
                    "columns": [
                        {"name": col.name, "type": col.data_type, "primary_key": col.primary_key}
                        for col in table.columns
                    ],
                }
                for table in schema.tables
            ],
            "relationships": relationships,
        }

    filter_values_cache: dict[tuple[str, str, str, str], tuple[float, list[str], bool]] = {}

    @app.get("/api/databases/{db_id}/filter-values", response_model=FilterValuesResponse)
    async def filter_values(
        db_id: str, table: str, column: str, search: str | None = None
    ) -> FilterValuesResponse:
        """Bounded, read-only distinct values for one verified filter field.

        Authorization is fail-closed: the database must exist, the field must
        be a verified non-key, non-sensitive textual/date column of the live
        schema, and per-column semantic metadata may not mark it sensitive.
        High-cardinality fields use a bounded server-side ``search`` instead of
        presenting the first values as the complete domain. Values are cached
        for at most one minute in a bounded, process-local map.
        """
        import re as _re

        if db_id not in sqlite.list_ids() and (
            postgres is None or db_id not in postgres.configured_ids()
        ):
            raise HTTPException(status_code=404, detail="Database was not found or configured.")
        if not _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table) or not _re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", column
        ):
            raise HTTPException(status_code=400, detail="Unsupported table or column name.")
        search_term = (search or "").strip()
        if len(search_term) > 64:
            raise HTTPException(status_code=400, detail="Search term is too long.")
        database = (
            sqlite if db_id in sqlite.list_ids() else (postgres if postgres is not None else None)
        )
        if database is None:
            raise HTTPException(status_code=404, detail="Database was not found or configured.")
        try:
            schema = await asyncio.to_thread(database.inspect, db_id)
        except DatabaseError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        table_info = next((item for item in schema.tables if item.name == table), None)
        column_info = (
            next((item for item in table_info.columns if item.name == column), None)
            if table_info is not None
            else None
        )
        if column_info is None or column_info.primary_key:
            raise HTTPException(status_code=404, detail="Unknown filter field.")
        physical = column_info.data_type.casefold().split("(", 1)[0]
        if not (
            physical.startswith(("text", "varchar", "char"))
            or physical in {"date", "datetime", "timestamp"}
        ):
            raise HTTPException(status_code=400, detail="Only text or date fields support values.")
        # Authorization: verified column metadata (profiles) may retire a
        # field from value discovery even when the raw schema still lists it.
        profile = None
        try:
            profile = await asyncio.to_thread(profiles.load, schema.dialect, db_id)
        except (DatabaseError, OSError, ValueError):
            profile = None
        for item in profile.columns if profile else []:
            if item.table == table and item.column == column:
                if item.semantic_type in _FILTER_FORBIDDEN_SEMANTIC_TYPES:
                    raise HTTPException(
                        status_code=403,
                        detail="This field is not available for value discovery.",
                    )
                break
        if _re.search(_FORBIDDEN_NAME_PATTERN, (table + "." + column).casefold()):
            raise HTTPException(
                status_code=403,
                detail="This field is not available for value discovery.",
            )
        cache_key = (db_id, table, column, search_term)
        cached = filter_values_cache.get(cache_key)
        if cached is not None and monotonic() - cached[0] < 60:
            _, cached_values, cached_more = cached
            return FilterValuesResponse(
                db_id=db_id,
                table=table,
                column=column,
                search=search_term or None,
                values=cached_values,
                more_available=cached_more,
                cached=True,
            )
        if search_term:
            # Treat the user's text literally; it is never SQL or a LIKE pattern.
            placeholder = "?" if schema.dialect == "sqlite" else "%s"
            predicate = (
                f'instr(lower(CAST("{column}" AS TEXT)), lower({placeholder})) > 0'
                if schema.dialect == "sqlite"
                else f'strpos(lower(CAST("{column}" AS TEXT)), lower({placeholder})) > 0'
            )
            sql = (
                f'SELECT DISTINCT CAST("{column}" AS TEXT) FROM "{table}" '
                f'WHERE "{column}" IS NOT NULL AND {predicate} ORDER BY 1 LIMIT 101'
            )
            parameters: tuple[str, ...] = (search_term,)
        else:
            sql = (
                f'SELECT DISTINCT CAST("{column}" AS TEXT) FROM "{table}" '
                f'WHERE "{column}" IS NOT NULL ORDER BY 1 LIMIT 101'
            )
            parameters = ()
        try:
            _, rows, _ = await asyncio.to_thread(
                database.execute,
                db_id,
                sql,
                max_rows=101,
                timeout_seconds=2.0,
                parameters=parameters,
            )
        except DatabaseError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        values: list[str] = [str(row[0]) for row in rows if row and row[0] is not None]
        more_available = len(values) > 100
        if len(filter_values_cache) >= 256:
            filter_values_cache.clear()
        filter_values_cache[cache_key] = (monotonic(), values[:100], more_available)
        return FilterValuesResponse(
            db_id=db_id,
            table=table,
            column=column,
            search=search_term or None,
            values=values[:100],
            more_available=more_available,
            cached=False,
        )

    @app.get("/api/analytics-metrics")
    async def analytics_metrics() -> dict[str, int]:
        """Aggregate planner decision counts for this process (no turns recorded)."""
        return metrics_ledger().summarize()

    @app.get("/api/databases/{db_id}/analytics-capabilities")
    async def analytics_capabilities(db_id: str) -> dict[str, Any]:
        """Expose a compact, read-only analytical capability summary."""
        if db_id in sqlite.list_ids():
            dialect = "sqlite"
            schema = sqlite.inspect(db_id)
        elif postgres is not None and db_id in postgres.configured_ids():
            dialect = "postgres"
            schema = postgres.inspect(db_id)
        else:
            raise HTTPException(status_code=404, detail="Database was not found or configured.")

        profile = profiles.load(dialect, db_id)
        profile_by_column = (
            {(item.table, item.column): item for item in profile.columns} if profile else {}
        )
        measures: list[dict[str, Any]] = []
        dimensions: list[dict[str, Any]] = []
        time_columns: list[dict[str, Any]] = []
        business_entities: set[str] = set()
        numeric_types = {"integer", "real", "numeric", "decimal", "float", "double"}
        for table in schema.tables:
            business_entities.add(table.name)
            for column in table.columns:
                column_profile = profile_by_column.get((table.name, column.name))
                semantic_type = column_profile.semantic_type if column_profile else None
                field = {
                    "name": column.name,
                    "table": table.name,
                    "data_type": column.data_type,
                    "semantic_type": semantic_type,
                    "description": column.description,
                }
                physical_type = column.data_type.casefold().split("(", 1)[0]
                if semantic_type in {"date", "datetime"} or any(
                    token in column.name.casefold() for token in ("date", "time", "month", "year")
                ):
                    time_columns.append(field)
                elif semantic_type == "numeric" or any(
                    numeric_type in physical_type for numeric_type in numeric_types
                ):
                    if not column.primary_key and semantic_type != "identifier":
                        measures.append(field)
                elif semantic_type not in {"secret", "email", "phone", "postal_address"}:
                    dimensions.append(field)

        glossary = glossaries.load(db_id)
        available_kpis = []
        if glossary is not None:
            for term in glossary.terms:
                if term.formula or term.structural_formula:
                    available_kpis.append(
                        {
                            "name": term.term,
                            "description": term.definition,
                            "synonyms": term.synonyms,
                            "columns": term.columns,
                            "source": "glossary",
                        }
                    )
                if term.core:
                    business_entities.add(term.term)
        return {
            "db_id": db_id,
            "dialect": dialect,
            "tables": [table.name for table in schema.tables],
            "business_entities": sorted(business_entities),
            "measures": measures[:200],
            "dimensions": dimensions[:200],
            "time_columns": time_columns[:100],
            "available_kpis": available_kpis[:100],
        }

    @app.get("/api/databases/{db_id}/semantic-catalog")
    async def semantic_catalog(db_id: str) -> dict[str, Any]:
        """Governed semantic catalog composed with live schema and profile status."""
        if db_id in sqlite.list_ids():
            schema = await asyncio.to_thread(sqlite.inspect, db_id)
        elif postgres is not None and db_id in postgres.configured_ids():
            schema = await asyncio.to_thread(postgres.inspect, db_id)
        else:
            raise HTTPException(status_code=404, detail="Database was not found or configured.")
        try:
            profile = profiles.load(schema.dialect, db_id)
        except (OSError, ValueError):
            profile = None
            logger.warning("Could not read schema profile for %s", db_id, exc_info=True)
        document = semantic_catalogs.load(db_id)
        if document is None:
            raise HTTPException(
                status_code=404,
                detail="No semantic catalog document is registered for this database.",
            )
        return compose_document(document, schema, profile)

    @app.get("/api/databases/{db_id}/semantic-catalog/verification")
    async def semantic_catalog_verification(db_id: str) -> dict[str, Any]:
        """Deterministic evidence probes for the registered catalog document."""
        if db_id in sqlite.list_ids():
            schema = await asyncio.to_thread(sqlite.inspect, db_id)
            runner = sqlite_runner(sqlite, db_id)
        elif postgres is not None and db_id in postgres.configured_ids():
            schema = await asyncio.to_thread(postgres.inspect, db_id)
            runner = postgres_runner(postgres, db_id)
        else:
            raise HTTPException(status_code=404, detail="Database was not found or configured.")
        document = semantic_catalogs.load(db_id)
        if document is None:
            raise HTTPException(
                status_code=404,
                detail="No semantic catalog document is registered for this database.",
            )
        report = await asyncio.to_thread(verify_catalog, document, schema, runner, schema.dialect)
        return report.model_dump()

    @app.post("/api/check", response_model=CheckResponse)
    async def check(request: CheckRequest) -> CheckResponse:
        if (
            request.execute
            and os.environ.get("TEXT2SQL_ENABLE_ADMIN_CHECK_EXECUTION", "false").casefold()
            != "true"
        ):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Direct SQL execution is disabled. Use /api/chat or explicitly enable the "
                    "administrative check endpoint."
                ),
            )
        return await asyncio.to_thread(active_agent.check, request)

    async def process_chat(request: ChatRequest) -> ChatResponse:
        if enforce_model_catalog and not supports_model(request.provider, request.model):
            raise HTTPException(
                status_code=422,
                detail=f"Model {request.model!r} is not allowed for {request.provider}.",
            )
        if (
            enforce_model_catalog
            and request.context_provider
            and request.context_model
            and not supports_model(request.context_provider, request.context_model)
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Context model {request.context_model!r} is not allowed for "
                    f"{request.context_provider}."
                ),
            )
        started = perf_counter()
        analytics_usage = TokenUsage()
        analytics_ledger: list[CallLedgerEntry] = []
        visualization_usage = TokenUsage()
        governed_metric_evidence: str | None = None
        request_budget = RequestBudget(
            timeout_seconds=float(os.environ.get("TEXT2SQL_REQUEST_TIMEOUT_SECONDS", "180")),
            max_model_calls=int(os.environ.get("TEXT2SQL_REQUEST_MAX_MODEL_CALLS", "6")),
            max_database_calls=int(os.environ.get("TEXT2SQL_REQUEST_MAX_DATABASE_CALLS", "24")),
        )
        set_session_stage(request.session_id, "conversation")
        previous = conversations.get(request.session_id)
        has_matching_state = previous is not None and previous.db_id == request.db_id
        viz_intent = parse_intent(request.message, has_previous=has_matching_state)
        if request.visualization is True and not viz_intent.requested:
            viz_intent = viz_intent.model_copy(update={"requested": True})
        if viz_intent.requested and viz_intent.followup and previous and previous.last_sql:
            prior_response = recent_results.get((request.session_id, request.db_id))
            if (
                request.parent_run_id
                and prior_response
                and (prior_response.run_id != request.parent_run_id)
            ):
                prior_response = None
            source = prior_response.generation if prior_response else None
            if source is None or source.sql != previous.last_sql or source.truncated:
                candidates = (
                    [runs.get(request.parent_run_id, request.session_id)]
                    if request.parent_run_id
                    else runs.list_sessions(request.session_id)
                )
                for record in candidates:
                    if record is None:
                        continue
                    if record.db_id != request.db_id or record.sql != previous.last_sql:
                        continue
                    if record.truncated or record.row_count != len(record.rows_preview):
                        break
                    raw = record.response_payload.get("generation")
                    if raw:
                        source = GenerateResponse.model_validate(raw)
                    break
            if source and source.accepted and not source.truncated:
                explanation = (
                    prior_response.presentation.summary
                    if prior_response and prior_response.presentation
                    else f"The stored result contains {source.row_count} rows."
                )
                chart_presentation = ResultPresentation(summary=explanation, source="fallback")
                # --- Result Reusability Gate: decided here, before any chart
                # planning. A renderable chart that answers a different
                # analytical request than the one asked is a wrong answer, so
                # metric identities, dimensions, grain and result completeness
                # gate every reuse of the stored result.
                analytics_started = perf_counter()
                proposal = None
                # Governed derived metrics: only definitions approved in the
                # semantic catalog are recognized; formulas are never invented.
                approved_metrics: dict[str, Any] = {}
                try:
                    catalog_document = semantic_catalogs.load(request.db_id)
                except (ValueError, OSError):
                    catalog_document = None
                if catalog_document is not None:
                    approved_metrics = {
                        entry.id: entry
                        for entry in catalog_document.metrics
                        if entry.status == "approved"
                    }
                if planner_enabled():
                    try:
                        proposal = plan_analytics(
                            request.message,
                            viz_intent=viz_intent,
                            previous=previous,
                            db_id=request.db_id,
                            approved_metrics=approved_metrics or None,
                        )
                    except Exception:  # noqa: BLE001 - planner proposals never abort a chart turn
                        proposal = None
                # Ambiguous deterministic interpretation gets one flagged,
                # validated model refinement; obvious chart mechanics never
                # reach the LLM.
                if (
                    planner_enabled()
                    and planner_llm_enabled()
                    and proposal is not None
                    and plan_needs_refinement(proposal)
                ):
                    known_fields = {name.casefold() for name in source.columns or []}
                    known_fields |= {name.casefold() for name in previous.last_columns}
                    deterministic_plan = proposal
                    model_calls_before = request_budget.model_calls
                    proposal, refine_usage, refine_ms = await refine_plan_with_model(
                        turn_completers[request.provider],
                        provider=request.provider,
                        model=request.model,
                        message=request.message,
                        previous=previous,
                        plan=proposal,
                        budget=request_budget,
                    )
                    analytics_usage = _add_usage(analytics_usage, refine_usage)
                    if request_budget.model_calls > model_calls_before:
                        analytics_ledger.append(
                            CallLedgerEntry(
                                component="analytics_planner",
                                kind="MODEL",
                                status="SUCCEEDED" if proposal.source == "model" else "FAILED",
                                provider=request.provider,
                                requested_model=request.model,
                                effective_model=request.model,
                                input_tokens=refine_usage.input_tokens,
                                output_tokens=refine_usage.output_tokens,
                                latency_ms=refine_ms,
                            )
                        )
                    invented_derived = proposal.source == "model" and bool(
                        set(proposal.requested_analysis.derived_measures) - set(approved_metrics)
                    )
                    if proposal.source == "model" and (
                        not plan_fields_known(proposal, known_fields) or invented_derived
                    ):
                        # An invented dimension or unverified derived formula is
                        # a failed refinement, never authorization to query
                        # unverified schema.
                        proposal = deterministic_plan.model_copy(update={"source": "fallback"})
                analytics_planning_ms = round((perf_counter() - analytics_started) * 1000)
                if not planner_enabled():
                    metrics_ledger().record(
                        PlannerDecisionRecord(
                            recorded_at=_planner_now(),
                            session_id=request.session_id,
                            db_id=request.db_id,
                            source="disabled",
                            latency_ms=analytics_planning_ms,
                        )
                    )
                requirement = None
                requery_requested = False
                if proposal is not None:
                    approved_requested = [
                        approved_metrics[metric_id]
                        for metric_id in proposal.requested_analysis.derived_measures
                        if metric_id in approved_metrics
                    ]
                    if approved_requested:
                        governed_metric_evidence = "Approved metric definitions:\n" + "\n".join(
                            f"{metric.id}: {metric.formula}; source={metric.source_relation}; "
                            f"required_fields={', '.join(metric.required_fields)}"
                            for metric in approved_requested
                        )
                    requirement = validate_reusability(
                        proposal,
                        stored_columns=source.columns,
                        stored_rows=source.rows,
                        stored_row_count=source.row_count,
                        stored_truncated=source.truncated,
                        approved_metrics=approved_metrics or None,
                    )
                    if requirement.decision == "requery_database":
                        requery_requested = True
                if planner_enabled():
                    metrics_ledger().record(
                        PlannerDecisionRecord(
                            recorded_at=_planner_now(),
                            session_id=request.session_id,
                            db_id=request.db_id,
                            source=(proposal.source if proposal is not None else "fallback"),
                            operation=proposal.operation if proposal else None,
                            reusability_decision=(
                                requirement.decision if requirement is not None else None
                            ),
                            requery=requery_requested,
                            latency_ms=analytics_planning_ms,
                            planner_llm_enabled=planner_llm_enabled(),
                        )
                    )
                if not requery_requested:
                    if requirement is not None and requirement.decision == "need_clarification":
                        clarification = requirement.reason
                        return ChatResponse(
                            session_id=request.session_id,
                            operation="VISUALIZE",
                            resolved_question=previous.resolved_question,
                            state=previous,
                            message=clarification,
                            clarification_required=True,
                            clarification_question=clarification,
                            timings_ms={
                                "analytics_planning": analytics_planning_ms,
                                "total": round((perf_counter() - started) * 1000),
                            },
                            token_usage=analytics_usage,
                            call_ledger=analytics_ledger,
                        )
                    try:
                        spec = None
                        if len(source.columns) > 2 and viz_recommender_llm_enabled():
                            model_calls_before = request_budget.model_calls
                            proposals, model_usage, model_ms = await refine_charts_with_model(
                                turn_completers.get(request.provider),
                                provider=request.provider,
                                model=request.model,
                                question=request.message,
                                columns=source.columns,
                                rows=source.rows,
                                budget=request_budget,
                                required_chart_type=(
                                    viz_intent.chart_type if viz_intent.explicit_type else None
                                ),
                            )
                            analytics_usage = _add_usage(analytics_usage, model_usage)
                            if request_budget.model_calls > model_calls_before:
                                analytics_ledger.append(
                                    CallLedgerEntry(
                                        component="visualization_recommendation",
                                        kind="MODEL",
                                        status="SUCCEEDED" if proposals else "FAILED",
                                        provider=request.provider,
                                        requested_model=request.model,
                                        effective_model=request.model,
                                        input_tokens=model_usage.input_tokens,
                                        output_tokens=model_usage.output_tokens,
                                        latency_ms=model_ms,
                                    )
                                )
                            if proposals is not None:
                                spec = materialize_recommendation(
                                    proposals.primary_chart,
                                    question=request.message,
                                    columns=source.columns,
                                    rows=source.rows,
                                    truncated=False,
                                )
                        if spec is None:
                            spec = plan_chart(
                                viz_intent,
                                question=request.message,
                                columns=source.columns,
                                rows=source.rows,
                                truncated=source.truncated,
                            )
                        chart_presentation = chart_presentation.model_copy(
                            update={
                                "chart_type": spec.chart_type,
                                "chart_spec": spec,
                                "x": spec.x,
                                "y": spec.y,
                                "title": spec.title,
                                "x_label": spec.x_label,
                                "y_label": spec.y_label,
                                "number_format": spec.number_format,
                                "time_format": spec.time_format,
                            }
                        )
                        message = "Charted the stored result without running SQL again."
                    except VisualizationError as exc:
                        message = (
                            requirement.reason
                            if requirement is not None
                            and requirement.decision not in {"reuse_result", "unsupported"}
                            else str(exc)
                        )
                        chart_presentation = chart_presentation.model_copy(
                            update={"chart_reason": message}
                        )
                    return ChatResponse(
                        session_id=request.session_id,
                        operation="VISUALIZE",
                        resolved_question=previous.resolved_question,
                        state=previous,
                        generation=source,
                        presentation=chart_presentation,
                        message=message,
                        previous_sql=previous.last_sql,
                        timings_ms={
                            "analytics_planning": analytics_planning_ms,
                            "total": round((perf_counter() - started) * 1000),
                        },
                        token_usage=analytics_usage,
                        call_ledger=analytics_ledger,
                    )
                # Fall through: the gate asked for new data, so the proposed
                # semantic changes route through the accepted text-to-SQL
                # pipeline with the chart output hint.
            else:
                # An incomplete saved preview cannot support a trustworthy chart.
                clarification = (
                    "The complete previous result is unavailable. "
                    "Ask the original question again with the plot request."
                )
                return ChatResponse(
                    session_id=request.session_id,
                    operation="VISUALIZE",
                    resolved_question=previous.resolved_question,
                    state=previous,
                    message=clarification,
                    clarification_required=True,
                    clarification_question=clarification,
                    timings_ms={"total": round((perf_counter() - started) * 1000)},
                )
        conversation_started = perf_counter()
        conversation_usage = TokenUsage()
        interpretation: TurnInterpretation | None = None
        if previous is not None and requires_model_interpretation(
            request.message,
            has_matching_state,
            request.feedback_category,
        ):
            try:
                interpretation, conversation_usage = await interpret_turn_detailed(
                    turn_completers[request.provider],
                    provider=request.provider,
                    model=request.model,
                    message=request.message,
                    previous=previous,
                )
            except (ModelError, ValueError):
                fallback_operation = classify_operation(
                    request.message,
                    has_matching_state,
                    request.feedback_category,
                )
                interpretation = TurnInterpretation(
                    operation=fallback_operation,
                    depends_on_previous=fallback_operation not in {"NEW_QUERY", "RESET_CONTEXT"},
                    resolved_instruction=request.message,
                    correction_type=request.feedback_category,
                    target=intent_target(request.message, fallback_operation),
                    confidence=0.0,
                    source="fallback",
                    provider=request.provider,
                    model=request.model,
                )
        if interpretation is None:
            rule_operation = classify_operation(
                request.message,
                has_matching_state,
                request.feedback_category,
            )
            interpretation = TurnInterpretation(
                operation=rule_operation,
                depends_on_previous=rule_operation not in {"NEW_QUERY", "RESET_CONTEXT"},
                resolved_instruction=request.message,
                correction_type=request.feedback_category,
                target=intent_target(request.message, rule_operation),
                confidence=1.0,
                source="rules",
            )
        conversation_ms = round((perf_counter() - conversation_started) * 1_000)
        conversation_ledger = (
            [
                CallLedgerEntry(
                    component="conversation_interpretation",
                    kind="MODEL",
                    status="SUCCEEDED",
                    provider=request.provider,
                    requested_model=request.model,
                    effective_model=request.model,
                    input_tokens=conversation_usage.input_tokens,
                    output_tokens=conversation_usage.output_tokens,
                    latency_ms=conversation_ms,
                )
            ]
            if interpretation.source == "model"
            else []
        )
        if interpretation.operation in STATE_REQUIRED_OPERATIONS and not has_matching_state:
            clarification = _missing_state_message(interpretation.operation)
            return ChatResponse(
                session_id=request.session_id,
                operation=interpretation.operation,
                resolved_question=request.message,
                conversation_interpretation=interpretation,
                state=previous,
                message=clarification,
                clarification_required=True,
                clarification_question=clarification,
                token_usage=conversation_usage,
                call_ledger=conversation_ledger,
                timings_ms={
                    "conversation_interpretation": conversation_ms,
                    "total": round((perf_counter() - started) * 1_000),
                },
            )
        if interpretation.source in {"model", "fallback"} and interpretation.confidence < 0.65:
            clarification = (
                "Should I modify the previous query, or treat your message as a new question?"
            )
            return ChatResponse(
                session_id=request.session_id,
                operation=interpretation.operation,
                resolved_question=previous.resolved_question if previous else request.message,
                conversation_interpretation=interpretation,
                state=previous,
                generation=None,
                message=clarification,
                clarification_required=True,
                clarification_question=clarification,
                token_usage=conversation_usage,
                call_ledger=conversation_ledger,
                timings_ms={
                    "conversation_interpretation": conversation_ms,
                    "total": round((perf_counter() - started) * 1_000),
                },
            )
        if has_matching_state and interpretation.operation == "NEW_QUERY":
            message = (
                "This conversation is for follow-ups to its original question. "
                "Start a new conversation for an independent question."
            )
            return ChatResponse(
                session_id=request.session_id,
                operation="NEW_QUERY",
                resolved_question=request.message,
                conversation_interpretation=interpretation,
                state=previous,
                message=message,
                clarification_required=True,
                clarification_question=message,
                token_usage=conversation_usage,
                call_ledger=conversation_ledger,
                timings_ms={
                    "conversation_interpretation": conversation_ms,
                    "total": round((perf_counter() - started) * 1_000),
                },
            )
        operation, pending = resolve_turn(
            request.session_id,
            request.db_id,
            request.message,
            previous,
            request.feedback_category,
            interpretation,
        )
        if operation == "RESET_CONTEXT":
            conversations.reset(request.session_id)
            return ChatResponse(
                session_id=request.session_id,
                operation="RESET_CONTEXT",
                resolved_question="",
                conversation_interpretation=interpretation,
                message="Conversation context was reset.",
                token_usage=conversation_usage,
                call_ledger=conversation_ledger,
                timings_ms={
                    "conversation_interpretation": conversation_ms,
                    "total": round((perf_counter() - started) * 1_000),
                },
            )
        assert pending is not None
        # Versioned presentation state: an analytical correction that follows a
        # chart-producing turn keeps the chart as the primary answer. The mode
        # comes from backend conversation state, never from a client heuristic.
        last_accepted = recent_results.get((request.session_id, request.db_id))
        last_chart_spec = (
            last_accepted.presentation.chart_spec
            if last_accepted and last_accepted.presentation
            else None
        )
        if (
            has_matching_state
            and last_chart_spec is not None
            and not viz_intent.requested
            and operation in ANALYTICAL_CORRECTION_OPERATIONS
        ):
            stable_types = {"line", "bar", "area", "step_line", "horizontal_bar"}
            viz_intent = viz_intent.model_copy(
                update={
                    "requested": True,
                    "followup": False,
                    "chart_type": (
                        last_chart_spec.chart_type
                        if last_chart_spec.chart_type in stable_types
                        else None
                    ),
                }
            )
        for job in chat_jobs.values():
            if job.get("session_id") == request.session_id and job.get("status") == "running":
                job["understanding"] = {
                    "operation": interpretation.operation,
                    "resolved_instruction": pending.resolved_question,
                    "source": interpretation.source,
                    "confidence": interpretation.confidence,
                    "changes": [item.model_dump() for item in interpretation.changes],
                }
        if operation in EXPLANATION_OPERATIONS:
            explanation_started = perf_counter()
            explanation_usage = TokenUsage()
            explanation_model_succeeded = False
            try:
                explanation, explanation_usage = await explain_turn_detailed(
                    turn_completers[request.provider],
                    model=request.model,
                    operation=operation,
                    client_message=request.message,
                    state=pending,
                    dialect=("sqlite" if request.db_id in sqlite.list_ids() else "postgres"),
                )
                explanation_model_succeeded = True
            except (ModelError, ValueError):
                explanation = deterministic_explanation(
                    operation,
                    pending,
                    "sqlite" if request.db_id in sqlite.list_ids() else "postgres",
                )
            return ChatResponse(
                session_id=request.session_id,
                operation=operation,
                resolved_question=pending.resolved_question,
                conversation_interpretation=interpretation,
                state=pending,
                message="Explained the previous query without generating or changing SQL.",
                explanation=explanation,
                provenance=[
                    "accepted SQL",
                    "SQLGlot-extracted SQL facts",
                    "verified schema and relationship context",
                    "recorded result or failure metadata",
                ],
                token_usage=_add_usage(conversation_usage, explanation_usage),
                call_ledger=[
                    *conversation_ledger,
                    CallLedgerEntry(
                        component=(
                            "explanation" if explanation_model_succeeded else "explanation_fallback"
                        ),
                        kind="MODEL" if explanation_model_succeeded else "TOOL",
                        status="SUCCEEDED",
                        provider=request.provider if explanation_model_succeeded else None,
                        requested_model=request.model if explanation_model_succeeded else None,
                        effective_model=request.model if explanation_model_succeeded else None,
                        input_tokens=explanation_usage.input_tokens,
                        output_tokens=explanation_usage.output_tokens,
                        latency_ms=round((perf_counter() - explanation_started) * 1_000),
                    ),
                ],
                timings_ms={
                    "conversation_interpretation": conversation_ms,
                    "explanation": round((perf_counter() - explanation_started) * 1_000),
                    "total": round((perf_counter() - started) * 1_000),
                },
            )
        execution = await question_service.execute_question(
            request_budget=request_budget,
            question=pending.resolved_question,
            db_id=request.db_id,
            evidence="\n\n".join(
                item for item in (request.evidence, governed_metric_evidence) if item
            )
            or None,
            visualization_requirement=sql_output_requirement(viz_intent),
            provider=request.provider,
            model=request.model,
            context_mode=request.context_mode,
            context_provider=request.context_provider,
            context_model=request.context_model,
            execute=request.execute,
            max_rows=request.max_rows,
            semantic_contract=(
                previous.semantic_contract
                if operation == "OPTIMIZE" and previous and previous.semantic_contract
                else None
            ),
            previous_intent=(
                previous.semantic_contract
                if operation != "NEW_QUERY" and previous and previous.semantic_contract
                else None
            ),
            previous_sql=(
                previous.last_sql
                if operation in {"OPTIMIZE", "CHECK_CORRECTNESS"} and previous
                else None
            ),
            previous_approved_tables=(
                previous.approved_tables
                if operation in {"OPTIMIZE", "CHECK_CORRECTNESS"} and previous
                else None
            ),
            optimization_required=operation == "OPTIMIZE",
            correctness_review=operation == "CHECK_CORRECTNESS",
            progress=lambda stage: set_session_stage(request.session_id, stage),
        )
        generated = execution.generated
        proposed_tables = execution.approved_tables
        planner_usage = execution.planner_usage
        routing_ms = execution.routing_ms
        planning_ms = execution.planning_ms
        generation_ms = execution.generation_ms
        response_state: ConversationState | None
        human_review: HumanReviewRequest | None = None
        if generated.accepted:
            pending = pending.model_copy(
                update={
                    "semantic_contract": generated.semantic_contract,
                    "approved_tables": proposed_tables,
                    "last_sql": generated.sql,
                    "last_columns": generated.columns,
                    "last_row_count": generated.row_count,
                    "last_truncated": generated.truncated,
                    "last_model_context": generated.model_context or {},
                    "last_failure": None,
                    "last_failed_sql": None,
                }
            )
            if operation == "CHECK_CORRECTNESS" or (
                generated.recovery and generated.recovery.agent_action == "ESCALATE"
            ):
                # A review candidate is evidence, not accepted conversation state. Keep the
                # prior SQL authoritative unless a later explicit correction replaces it.
                response_state = previous
            else:
                try:
                    response_state = conversations.put(
                        pending,
                        expected_version=previous.version if previous is not None else None,
                    )
                except ConversationConflict as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                pending = response_state
            message = _conversational_answer(operation, generated)
            if generated.recovery and generated.recovery.mode in {
                "ZERO_RESULT",
                "NULL_RESULT",
            }:
                message = generated.recovery.diagnosis_summary or message
                if generated.recovery.agent_action == "ESCALATE":
                    human_review = HumanReviewRequest(
                        reason=message,
                        question="The result requires review before it is treated as accepted.",
                        options=[],
                        evidence=generated.recovery.evidence,
                    )
            if operation == "CHECK_CORRECTNESS" and previous and previous.last_sql:
                database = sqlite if request.db_id in sqlite.list_ids() else postgres
                # None means the comparison could not run (budget exhausted); that is not
                # the same as the candidate disagreeing with the accepted result.
                equivalent: bool | None = None
                if database is not None:
                    try:
                        request_budget.claim("database")
                    except RequestBudgetExceeded:
                        equivalent = None
                    else:
                        timeout_seconds = max(0.5, min(5.0, request_budget.remaining()))
                        try:
                            old_columns, old_rows, old_truncated = await asyncio.to_thread(
                                database.execute,
                                request.db_id,
                                previous.last_sql,
                                max_rows=request.max_rows,
                                timeout_seconds=timeout_seconds,
                            )
                            equivalent = (
                                not old_truncated
                                and not generated.truncated
                                and old_columns == generated.columns
                                and old_rows == generated.rows
                            )
                        except Exception:  # pragma: no cover - review must not break chat
                            equivalent = False
                if equivalent:
                    message = (
                        "An independently generated candidate returned the same bounded result. "
                        "This increases confidence but is not formal proof of business correctness."
                    )
                elif equivalent is None:
                    human_review = HumanReviewRequest(
                        reason=(
                            "The request budget was exhausted before the previous SQL could be "
                            "re-executed, so candidate equivalence is unverified."
                        ),
                        question="Which interpretation should be trusted before replacing the SQL?",
                        options=[],
                        evidence=generated.recovery.evidence if generated.recovery else [],
                    )
                else:
                    human_review = HumanReviewRequest(
                        reason=(
                            "The independent correctness candidate did not reproduce the previous "
                            "bounded output exactly."
                        ),
                        question="Which interpretation should be trusted before replacing the SQL?",
                        options=[],
                        evidence=generated.recovery.evidence if generated.recovery else [],
                    )
        else:
            message = _failure_message(generated)
            prior_for_database = previous if has_matching_state else None
            failure_state = (prior_for_database or pending).model_copy(
                update={
                    "last_failure": message,
                    "last_failed_sql": generated.attempts[-1].sql if generated.attempts else None,
                }
            )
            try:
                response_state = conversations.put(
                    failure_state,
                    expected_version=(
                        prior_for_database.version if prior_for_database is not None else None
                    ),
                )
            except ConversationConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if generated.termination_reason != "model_error":
                recovery = generated.recovery
                human_review = HumanReviewRequest(
                    reason=(
                        recovery.diagnosis_summary
                        if recovery and recovery.diagnosis_summary
                        else "Automated recovery could not establish a safe correction."
                    ),
                    question="Please clarify the intended request in the chat.",
                    options=[],
                    evidence=recovery.evidence if recovery else [],
                )
        presentation: ResultPresentation | None = None
        presentation_usage = TokenUsage()
        presentation_ledger = []
        if (
            generated.accepted
            and generated.rows
            and not human_review
            and operation not in {"CHECK_CORRECTNESS", "OPTIMIZE"}
            and not generated.recovery
        ):
            set_session_stage(request.session_id, "presentation")
            presentation, presentation_usage, presentation_entries = await present_result(
                turn_completers.get(request.provider),
                question=pending.resolved_question,
                sql=generated.sql or "",
                columns=generated.columns,
                rows=generated.rows,
                truncated=generated.truncated,
                model=request.model,
                provider=request.provider,
                budget=request_budget,
                visualization=viz_intent,
            )
            presentation_ledger.extend(presentation_entries)
        chart_recommendations = None
        suggested_filters = None
        if (
            generated.accepted
            and generated.rows
            and not generated.truncated
            and operation not in {"CHECK_CORRECTNESS", "OPTIMIZE"}
            and not human_review
        ):
            try:
                chart_recommendations = recommend_charts(
                    pending.resolved_question,
                    generated.columns,
                    generated.rows,
                    truncated=False,
                )
            except Exception:
                logger.warning("Chart recommendation failed", exc_info=True)
                chart_recommendations = None
            if (
                viz_intent.requested
                and len(generated.columns) > 2
                and viz_recommender_llm_enabled()
            ):
                try:
                    model_calls_before = request_budget.model_calls
                    model_recommended, viz_usage, viz_ms = await refine_charts_with_model(
                        turn_completers.get(request.provider),
                        provider=request.provider,
                        model=request.model,
                        question=pending.resolved_question,
                        columns=generated.columns,
                        rows=generated.rows,
                        budget=request_budget,
                        required_chart_type=(
                            viz_intent.chart_type if viz_intent.explicit_type else None
                        ),
                    )
                    client_ledger = (
                        [
                            CallLedgerEntry(
                                component="visualization_recommendation",
                                kind="MODEL",
                                status="SUCCEEDED" if model_recommended is not None else "FAILED",
                                provider=request.provider,
                                requested_model=request.model,
                                effective_model=request.model,
                                input_tokens=viz_usage.input_tokens,
                                output_tokens=viz_usage.output_tokens,
                                latency_ms=viz_ms,
                            )
                        ]
                        if request_budget.model_calls > model_calls_before
                        else []
                    )
                    chart_recommendations = merge_recommendations(
                        model_recommended, chart_recommendations
                    )
                    visualization_usage = _add_usage(visualization_usage, viz_usage)
                    presentation_ledger.extend(client_ledger)
                except Exception:
                    logger.warning("LLM chart recommendation failed", exc_info=True)
            if (
                presentation is not None
                and viz_intent.requested
                and chart_recommendations is not None
            ):
                try:
                    winning_spec = materialize_recommendation(
                        chart_recommendations.primary_chart,
                        question=pending.resolved_question,
                        columns=generated.columns,
                        rows=generated.rows,
                        truncated=False,
                    )
                    presentation = presentation.model_copy(
                        update={
                            "chart_spec": winning_spec,
                            "chart_type": winning_spec.chart_type,
                            "x": winning_spec.x,
                            "y": winning_spec.y,
                            "title": winning_spec.title,
                            "x_label": winning_spec.x_label,
                            "y_label": winning_spec.y_label,
                            "number_format": winning_spec.number_format,
                            "time_format": winning_spec.time_format,
                            "chart_reason": "",
                        }
                    )
                except VisualizationError:
                    logger.warning("Primary chart recommendation could not be materialized")
            database = (
                sqlite
                if request.db_id in sqlite.list_ids()
                else (postgres if postgres is not None else None)
            )
            if database is not None:
                try:
                    schema = await asyncio.to_thread(database.inspect, request.db_id)
                    profile = profiles.load(schema.dialect, request.db_id)
                    suggested_filters = discover_filters(
                        schema=schema,
                        columns=generated.columns,
                        rows=generated.rows,
                        truncated=False,
                        approved_tables=(
                            response_state.approved_tables or None if response_state else None
                        ),
                        profile=profile,
                        executed_sql=generated.sql,
                    )
                except (DatabaseError, OSError, ValueError):
                    logger.warning("Filter discovery failed", exc_info=True)
        response = ChatResponse(
            session_id=request.session_id,
            operation=operation,
            resolved_question=pending.resolved_question,
            conversation_interpretation=interpretation,
            state=response_state,
            generation=generated,
            presentation=presentation,
            message=message,
            human_review=human_review,
            chart_recommendations=chart_recommendations,
            suggested_filters=suggested_filters,
            provenance=[
                "current question and trusted evidence",
                "session-scoped conversation contract",
                "business glossary",
                "live schema and column profiles",
                "SQLGlot read-only safety validation",
            ],
            token_usage=_add_usage(
                _add_usage(conversation_usage, analytics_usage),
                _add_usage(
                    presentation_usage,
                    _add_usage(
                        visualization_usage, _add_usage(planner_usage, generated.token_usage)
                    ),
                ),
            ),
            call_ledger=[
                *analytics_ledger,
                *conversation_ledger,
                *generated.telemetry.call_ledger,
                *presentation_ledger,
            ],
            timings_ms={
                "conversation_interpretation": conversation_ms,
                "routing": routing_ms,
                "planning": planning_ms,
                "generation_validation_execution": generation_ms,
                "total": round((perf_counter() - started) * 1_000),
            },
        )
        return response

    def build_presentation_state(response: ChatResponse) -> PresentationState | None:
        """Derive the typed presentation state from a completed response.

        A validated chart wins; otherwise an accepted SQL answer is SQL mode.
        Clarifications and failed operations intentionally yield ``None`` so the
        previously accepted state is never overwritten by a bad turn.
        """

        if response.presentation and response.presentation.chart_spec is not None:
            spec = response.presentation.chart_spec
            return PresentationState(
                mode="chart",
                chart_type=spec.chart_type,
                x=spec.x,
                y=spec.y,
                series=spec.series,
                size=spec.size,
                title=spec.title,
                source_run_id=response.run_id,
                data_complete=not (response.generation and response.generation.truncated),
            )
        if response.generation and response.generation.accepted and response.generation.sql:
            return PresentationState(mode="sql", source_run_id=response.run_id)
        return response.presentation_state

    async def save_response(request: ChatRequest, response: ChatResponse) -> ChatResponse:
        if response.operation == "RESET_CONTEXT":
            recent_results.pop((request.session_id, request.db_id), None)
            return response
        run_id = uuid4().hex
        presentation_state = build_presentation_state(response)
        if presentation_state is not None and presentation_state.source_run_id is None:
            presentation_state = presentation_state.model_copy(update={"source_run_id": run_id})
        response = response.model_copy(
            update={
                "run_id": run_id,
                "turn_id": run_id,
                "recovery_events": recovery_events(response.generation, run_id),
                "presentation_state": presentation_state,
            }
        )
        try:
            record = await asyncio.to_thread(
                runs.record,
                build_run_record(
                    {
                        "response": response,
                        "request": request,
                        "message": request.message,
                        "latency_ms": response.timings_ms.get("total", 0),
                    }
                ),
            )
            saved_response = response.model_copy(
                update={
                    "conversation_id": record.conversation_id,
                    "parent_run_id": record.parent_run_id,
                    "sql_version": record.sql_version,
                    "history_saved": True,
                }
            )
            if (
                saved_response.generation
                and saved_response.generation.accepted
                and not saved_response.generation.truncated
            ):
                key = (request.session_id, request.db_id)
                recent_results.pop(key, None)
                recent_results[key] = saved_response
                if len(recent_results) > 100:
                    recent_results.pop(next(iter(recent_results)))
            return saved_response
        except Exception:  # History failures must not discard a completed query response.
            logger.warning("run_history_record_failed", exc_info=True)
            return response

    @app.post("/api/chat", response_model=ChatResponse)
    async def chat(request: ChatRequest) -> ChatResponse:
        if request.parent_run_id:
            parent = runs.get(request.parent_run_id, request.session_id)
            if parent is None or parent.db_id != request.db_id:
                raise HTTPException(
                    status_code=409, detail="The selected previous turn is unavailable."
                )
            if parent.state_snapshot is not None:
                current = conversations.get(request.session_id)
                try:
                    conversations.put(
                        parent.state_snapshot,
                        expected_version=current.version if current else None,
                    )
                except ConversationConflict as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
        previous = conversations.get(request.session_id)
        response = await process_chat(request)
        if previous and previous.db_id == request.db_id and response.operation != "NEW_QUERY":
            response = response.model_copy(update={"previous_sql": previous.last_sql})
        return await save_response(request, response)

    @app.post("/api/history/{run_id}/rerun", response_model=ChatResponse)
    async def rerun_history(run_id: str, session_id: str) -> ChatResponse:
        record = runs.get(run_id, session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Conversation turn not found.")
        if not record.accepted or not record.sql or record.state_snapshot is None:
            raise HTTPException(status_code=409, detail="This turn has no accepted SQL to rerun.")
        context = (record.response_payload.get("generation") or {}).get("model_context") or {}
        tables = context.get("tables") or context.get("execution_context", {}).get("tables") or {}
        allowed_tables = set(tables) or set(record.state_snapshot.approved_tables)
        if not allowed_tables:
            raise HTTPException(
                status_code=409, detail="This older turn has no verified table scope."
            )
        allowed_columns = (
            {table: set(info.get("columns") or []) for table, info in tables.items()}
            if tables
            else None
        )
        original = record.response_payload.get("generation") or {}
        dialect = original.get("dialect", "sqlite")
        budget = RequestBudget(15, max_model_calls=0, max_database_calls=2)
        started = perf_counter()
        try:
            checked = await budget.wait(
                asyncio.to_thread(
                    active_agent.check,
                    CheckRequest(
                        db_id=record.db_id,
                        sql=record.sql,
                        dialect=dialect,
                        execute=True,
                        max_rows=100,
                    ),
                    budget=budget,
                    allowed_tables=allowed_tables,
                    allowed_columns=allowed_columns,
                ),
                kind="work",
            )
        except (DatabaseError, RequestBudgetExceeded) as exc:
            checked = CheckResponse(
                db_id=record.db_id,
                dialect=dialect,
                validation=ValidationResult(valid=False, code="DATABASE_ERROR", message=str(exc)),
            )
        valid = checked.validation.valid
        elapsed = round((perf_counter() - started) * 1000)
        generated = GenerateResponse(
            db_id=record.db_id,
            question=record.resolved_question,
            dialect=dialect,
            provider=cast(ModelProvider, record.provider),
            model=record.model,
            strategy=route_question(record.resolved_question),
            sql=record.sql if valid else None,
            accepted=valid,
            execution_status="EXECUTABLE" if valid else "NOT_EXECUTED",
            columns=checked.columns,
            rows=checked.rows,
            row_count=checked.row_count,
            truncated=checked.truncated,
            model_context=context,
            attempts=[
                Attempt(
                    number=1,
                    sql=record.sql,
                    normalized_sql=record.sql,
                    validation=checked.validation,
                    latency_ms=elapsed,
                )
            ],
            termination_reason="accepted" if valid else "database_error",
            token_usage=TokenUsage(input_tokens=0, output_tokens=0),
        )
        response_state = record.state_snapshot
        if valid:
            current = conversations.get(session_id)
            response_state = conversations.put(
                response_state.model_copy(
                    update={
                        "last_sql": record.sql,
                        "last_columns": checked.columns,
                        "last_row_count": checked.row_count,
                        "last_truncated": checked.truncated,
                    }
                ),
                expected_version=current.version if current else None,
            )
        return await save_response(
            ChatRequest(
                session_id=session_id,
                db_id=record.db_id,
                message="Rerun the saved SQL.",
                parent_run_id=record.run_id,
                provider=cast(ModelProvider, record.provider),
                model=record.model,
            ),
            ChatResponse(
                session_id=session_id,
                operation="REFINE",
                resolved_question=record.resolved_question,
                state=response_state,
                generation=generated,
                message=(
                    f"Reran the saved SQL against current data: {checked.row_count} rows returned."
                    if valid
                    else "The saved SQL could not be rerun: " + checked.validation.message
                ),
                timings_ms={"total": elapsed},
                token_usage=TokenUsage(input_tokens=0, output_tokens=0),
            ),
        )

    async def run_chat_job(job_id: str, request: ChatRequest) -> None:
        job = chat_jobs[job_id]
        job["status"] = "running"
        job["stage"] = "conversation"
        job["events"].append({"stage": "conversation", "elapsed_ms": 0})
        try:
            response = await chat(request)
            job.update(
                status="completed",
                stage="completed",
                response=response.model_dump(mode="json"),
                finished_at=perf_counter(),
            )
        except asyncio.CancelledError:
            job.update(status="cancelled", stage="cancelled", finished_at=perf_counter())
            raise
        except Exception as exc:  # pragma: no cover - defensive job boundary
            job.update(status="failed", stage="failed", error=str(exc), finished_at=perf_counter())

    @app.post("/api/chat/jobs", status_code=202)
    async def start_chat_job(request: ChatRequest) -> dict[str, str]:
        cleanup_jobs()
        job_id = uuid4().hex
        chat_jobs[job_id] = {
            "job_id": job_id,
            "session_id": request.session_id,
            "status": "queued",
            "stage": "queued",
            "started_at": perf_counter(),
            "response": None,
            "error": None,
            "events": [],
        }
        chat_tasks[job_id] = asyncio.create_task(run_chat_job(job_id, request))
        return {"job_id": job_id, "status": "queued"}

    @app.get("/api/chat/jobs/{job_id}")
    async def get_chat_job(job_id: str) -> dict[str, Any]:
        cleanup_jobs()
        job = chat_jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Chat job was not found.")
        return {
            **job,
            "elapsed_ms": round(
                (float(job.get("finished_at", perf_counter())) - float(job["started_at"])) * 1_000
            ),
        }

    @app.delete("/api/chat/jobs/{job_id}")
    async def cancel_chat_job(job_id: str) -> dict[str, str]:
        cleanup_jobs()
        job = chat_jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Chat job was not found.")
        if job["status"] in {"completed", "failed", "cancelled"}:
            return {"job_id": job_id, "status": str(job["status"])}
        job.update(status="cancelled", stage="cancelled")
        task = chat_tasks.get(job_id)
        if task is not None:
            task.cancel()
        return {"job_id": job_id, "status": "cancelled"}

    return app


def _add_usage(first: TokenUsage, second: TokenUsage) -> TokenUsage:
    return TokenUsage(
        input_tokens=_add_known(first.input_tokens, second.input_tokens),
        output_tokens=_add_known(first.output_tokens, second.output_tokens),
        cache_read_tokens=_add_known(first.cache_read_tokens, second.cache_read_tokens),
        cache_creation_tokens=_add_known(first.cache_creation_tokens, second.cache_creation_tokens),
    )


def _add_known(first: int | None, second: int | None) -> int | None:
    if first is None and second is None:
        return None
    return (first or 0) + (second or 0)


def _missing_state_message(operation: str) -> str:
    if operation == "OPTIMIZE":
        return "I do not have a previous accepted query to optimize. Run a query first."
    if operation == "EXPLAIN_FAILURE":
        return "I do not have a recorded query failure to explain in this conversation."
    if operation in EXPLANATION_OPERATIONS:
        return "I do not have a previous query or result to explain. Run a query first."
    if operation == "CORRECTION":
        return "I do not have a previous request to correct. Ask the complete question first."
    return "Should I treat this as a new analytical question?"


def _conversational_answer(operation: str, generated: GenerateResponse) -> str:
    if operation == "OPTIMIZE":
        if generated.optimization and generated.optimization.status == "optimized":
            prefix = "I verified that the replacement is equivalent and measurably faster."
        elif generated.optimization and generated.optimization.status == "equivalent_not_faster":
            prefix = "The rewrite was equivalent but not faster, so I retained the original query."
        else:
            prefix = "No optimization passed the acceptance gate; I retained the previous query."
    elif operation == "CHECK_CORRECTNESS":
        prefix = "I ran an evidence-based correctness review and executed an independent candidate."
    elif operation == "CORRECTION":
        prefix = "I applied your correction and reran the query."
    elif operation == "NEW_QUERY":
        prefix = (
            "The SQL passed read-only safety checks and executed successfully; "
            "execution does not prove business correctness."
        )
    else:
        prefix = "I applied your follow-up to the previous request and reran the query."
    if len(generated.columns) == 1 and len(generated.rows) == 1:
        value = generated.rows[0][0]
        rendered = "NULL" if value is None else str(value)
        return f"{prefix} {generated.columns[0]}: {rendered}."
    return f"{prefix} I found {generated.row_count} result rows."


def _failure_message(generated: GenerateResponse) -> str:
    """Explain a failed turn, separating an unreachable model from rejected SQL."""
    if generated.termination_reason == "model_error":
        detail = generated.model_error or "the model returned no usable response"
        if detail.startswith("Model 2A"):
            return detail
        return f"The {generated.model} model could not be reached: {detail}"
    if generated.termination_reason == "context_error":
        return generated.model_error or "Required database context could not be verified."
    return "Query failed; previous conversation state was preserved."


app = create_app()
