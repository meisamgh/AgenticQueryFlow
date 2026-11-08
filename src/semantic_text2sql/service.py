"""Reusable non-conversation orchestration for grounded Text-to-SQL questions."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Literal, cast

from semantic_text2sql.agent import TextToSQLAgent
from semantic_text2sql.context_planner import (
    ContextPlannerCompleter,
    fallback_context_request,
    reconcile_context_contract,
    verify_context_request,
)
from semantic_text2sql.database import DatabaseError, DatabaseRegistry
from semantic_text2sql.formulas import apply_structural_formulas
from semantic_text2sql.glossary import GlossaryStore
from semantic_text2sql.graph_grounding import compact_grounding, resolve_metrics
from semantic_text2sql.historical import HistoricalQueryStore
from semantic_text2sql.hybrid_retrieval import HybridSchemaRetriever, metadata_requests
from semantic_text2sql.join_resolver import resolve_joins
from semantic_text2sql.kpi_resolution import build_kpi_grounding, resolve_kpi_candidates
from semantic_text2sql.models import (
    CallLedgerEntry,
    ContextRelationship,
    ContextRequest,
    GenerateRequest,
    GenerateResponse,
    HistoricalExample,
    KPICandidate,
    ModelProvider,
    PipelineTelemetry,
    PlannerMetadataRequirement,
    RetrievalTrace,
    SemanticContract,
    TokenUsage,
    VerifiedSchemaSnapshot,
)
from semantic_text2sql.ontology import LocalOntologyAdapter
from semantic_text2sql.postgres import PostgresRegistry
from semantic_text2sql.profiling import ProfileStore
from semantic_text2sql.recovery import RecoveryCoordinator, RecoveryTools, recovery_feedback
from semantic_text2sql.runtime import RequestBudget, RequestBudgetExceeded
from semantic_text2sql.semantic_catalog import SemanticCatalogStore
from semantic_text2sql.semantic_graph import SemanticGraphStore, compile_graph, graph_identity
from semantic_text2sql.strategy import route_question
from semantic_text2sql.validator import validate_sql

logger = logging.getLogger(__name__)
ContextMode = Literal["model1", "retrieval"]


@dataclass(frozen=True)
class QuestionExecution:
    generated: GenerateResponse
    approved_tables: list[str]
    semantic_contract: SemanticContract
    planner_usage: TokenUsage
    routing_ms: int
    planning_ms: int
    generation_ms: int


class TextToSQLService:
    """One shared retrieval-to-execution path for chat and analytical tools."""

    def __init__(
        self,
        *,
        database: DatabaseRegistry,
        postgres: PostgresRegistry | None = None,
        profiles: ProfileStore,
        glossaries: GlossaryStore,
        history: HistoricalQueryStore,
        retriever: HybridSchemaRetriever,
        agent: TextToSQLAgent,
        context_completers: Mapping[ModelProvider, ContextPlannerCompleter] | None = None,
        semantic_catalogs: SemanticCatalogStore | None = None,
        graph_store: SemanticGraphStore | None = None,
    ) -> None:
        self.database = database
        self.postgres = postgres
        self.profiles = profiles
        self.glossaries = glossaries
        self.history = history
        self.retriever = retriever
        self.agent = agent
        self.semantic_catalogs = semantic_catalogs
        self.graph_store = graph_store

    async def execute_question(
        self,
        *,
        question: str,
        db_id: str,
        provider: ModelProvider,
        model: str,
        evidence: str | None = None,
        visualization_requirement: str | None = None,
        context_mode: ContextMode = "retrieval",
        context_provider: ModelProvider | None = None,
        context_model: str | None = None,
        execute: bool = True,
        max_rows: int = 100,
        max_attempts: int = 3,
        semantic_contract: SemanticContract | None = None,
        previous_intent: SemanticContract | None = None,
        previous_sql: str | None = None,
        previous_approved_tables: list[str] | None = None,
        optimization_required: bool = False,
        correctness_review: bool = False,
        progress: Callable[[str], None] | None = None,
        request_budget: RequestBudget | None = None,
    ) -> QuestionExecution:
        routing_started = perf_counter()
        budget = request_budget or RequestBudget(
            timeout_seconds=float(os.environ.get("TEXT2SQL_REQUEST_TIMEOUT_SECONDS", "180")),
            max_model_calls=int(os.environ.get("TEXT2SQL_REQUEST_MAX_MODEL_CALLS", "6")),
            max_database_calls=int(os.environ.get("TEXT2SQL_REQUEST_MAX_DATABASE_CALLS", "24")),
        )
        if progress:
            progress("retrieval")
        database = self._database(db_id)
        schema = await budget.wait(asyncio.to_thread(database.inspect, db_id), kind="database")
        dialect = schema.dialect
        database_profile = await budget.wait(
            asyncio.to_thread(self.profiles.load, dialect, db_id), kind="work"
        )
        ontology = (LocalOntologyAdapter(schema, database_profile, self.semantic_catalogs,
                                         self.graph_store, self.glossaries)
                    if self.semantic_catalogs and self.graph_store else None)
        correctness_trace = None
        if correctness_review and previous_sql:
            correctness_trace = await RecoveryCoordinator(
                RecoveryTools(database, db_id, schema, database_profile, budget=budget,
                              ontology=ontology)
            ).ainvestigate(
                question=question,
                failed_sql=previous_sql,
                failure_code="CORRECTNESS_REVIEW_SCHEMA",
                failure_message="User requested evidence-based correctness verification.",
                allowed_tables=previous_approved_tables or [table.name for table in schema.tables],
                mode="CORRECTNESS",
                progress=progress,
                completer=self.agent.model,
                provider=os.environ.get("TEXT2SQL_RECOVERY_PROVIDER") or provider,
                model=os.environ.get("TEXT2SQL_RECOVERY_MODEL") or model,
                budget=budget,
            )
            evidence = "\n\n".join(
                item
                for item in (
                    evidence,
                    f"Previously accepted SQL:\n{previous_sql}",
                    recovery_feedback(correctness_trace),
                    "Generate an independent candidate; do not copy the previous SQL unless "
                    "the evidence supports the same solution.",
                )
                if item
            )
        value_alias_context = self.glossaries.value_alias_context(db_id, question)
        retrieval_evidence = (
            "\n".join(item for item in (evidence, value_alias_context) if item) or None
        )
        contract = semantic_contract or SemanticContract()
        if (correctness_trace and correctness_trace.agent_action == "REPAIR"
                and correctness_trace.ontology_metrics):
            contract = contract.model_copy(update={"approved_metrics": list({
                item.id: item for item in [*contract.approved_metrics,
                                          *correctness_trace.ontology_metrics]
            }.values())})
        graph_context = None
        graph_latency_ms: int | None = None
        catalog_relationships: list[ContextRelationship] = []
        graph_enabled = os.environ.get("TEXT2SQL_GRAPH_ENABLED", "true").lower() == "true"
        metric_grounding = (graph_enabled or bool(contract.approved_metrics)
                            or os.environ.get("TEXT2SQL_KPI_GROUNDING_ENABLED", "false").lower()
                            == "true")
        try:
            catalog = (self.semantic_catalogs.load(db_id)
                       if metric_grounding and self.semantic_catalogs else None)
        except (OSError, ValueError):
            logger.warning("semantic_catalog_unavailable db_id=%s", db_id)
            catalog = None
        metric_facts: list[dict[str, Any]] = []
        kpi_first = (graph_enabled and os.environ.get(
            "TEXT2SQL_KPI_FIRST_ENABLED", "false").casefold() == "true")
        glossary = self.glossaries.load(db_id)
        if metric_grounding:
            try:
                kpi_resolution = (resolve_kpi_candidates(
                    question, schema, catalog, glossary=glossary, encoder=self.retriever.encoder,
                ) if kpi_first else None)
                if kpi_resolution and kpi_resolution.status in {"AMBIGUOUS", "UNAVAILABLE"}:
                    raise ValueError("KPI_" + kpi_resolution.status + ": "
                                     + ", ".join(kpi_resolution.reasons))
                contract, metric_facts = resolve_metrics(
                    question, schema, catalog, contract,
                    metric_ids=kpi_resolution.selected_ids if kpi_resolution else None,
                    strict_dependencies=kpi_first,
                )
                if kpi_resolution and catalog:
                    if not kpi_resolution.selected_ids and contract.approved_metrics:
                        kpi_resolution.selected_ids = (
                            list(contract.kpi_grounding.resolution.selected_ids)
                            if contract.kpi_grounding else [m.id for m in contract.approved_metrics]
                        )
                        kpi_resolution.status = "RESOLVED"
                        kpi_resolution.candidates = [KPICandidate(
                            metric_id=key, method="accepted_state",
                            source="accepted_semantic_contract",
                            evidence=["Accepted identity revalidated against the current catalog"],
                            score=1, authoritative_identity=True,
                        ) for key in kpi_resolution.selected_ids]
                    grounding = build_kpi_grounding(kpi_resolution, contract, catalog, question,
                                                   schema=schema)
                    contract = contract.model_copy(update={
                        "kpi_grounding": grounding,
                        "required_columns": grounding.mandatory_columns,
                        "grain": (grounding.requested_dimensions or contract.grain),
                        "requires_grouping": bool(grounding.requested_dimensions)
                                             or contract.requires_grouping,
                    })
            except ValueError as exc:
                return QuestionExecution(generated=GenerateResponse(
                    db_id=db_id, question=question, provider=provider, model=model, dialect=dialect,
                    strategy=route_question(question), semantic_contract=contract, sql=None,
                    accepted=False, termination_reason="context_error", model_error=str(exc)),
                    approved_tables=[], semantic_contract=contract, planner_usage=TokenUsage(),
                    routing_ms=round((perf_counter() - routing_started) * 1000),
                    planning_ms=0, generation_ms=0)
            if metric_facts:
                retrieval_evidence = "\n".join(filter(None, [retrieval_evidence,
                    "Approved metric columns: " + ", ".join(contract.required_columns)]))[:8000]
        retrieval_trace: RetrievalTrace | None = None

        if optimization_required and previous_approved_tables:
            proposed_tables = previous_approved_tables
            planner_schema = schema.model_copy(
                update={
                    "tables": [table for table in schema.tables if table.name in proposed_tables],
                    "relationships": [
                        item
                        for item in schema.relationships
                        if item.from_table in proposed_tables and item.to_table in proposed_tables
                    ],
                }
            )
            context_request = fallback_context_request(contract)
        else:
            historical_schema_evidence = self.history.schema_evidence(
                question,
                db_id,
                schema,
                top_k=3,
                min_score=float(os.environ.get("TEXT2SQL_HISTORY_ML_MIN_SCORE", "0.65")),
            )
            pin = contract.kpi_grounding if kpi_first and metric_facts else None
            kpi_arguments: dict[str, Any] = ({
                "mandatory_columns": pin.mandatory_columns,
                "mandatory_tables": pin.mandatory_tables,
                "defer_join_expansion": True,
                "kpi_grounding": pin,
            } if pin else {})
            planner_schema, retrieval_selection, retrieval_trace = await budget.wait(
                asyncio.to_thread(
                    self.retriever.retrieve,
                    question,
                    retrieval_evidence,
                    schema,
                    database_profile,
                    glossary,
                    historical_schema_evidence,
                    **kpi_arguments,
                ),
                kind="work",
            )
            proposed_tables = retrieval_selection.tables
            metadata = metadata_requests(question, planner_schema, database_profile)
            context_request = ContextRequest(
                tables=retrieval_selection.tables,
                columns=retrieval_selection.columns,
                business_concepts=sorted(self.glossaries.relevant_concept_ids(db_id, question)),
                metadata_requirements=[
                    PlannerMetadataRequirement(kind=cast(Any, kind), targets=[target])
                    for kind, target in metadata
                ],
            )
        try:
            semantic_context = (compact_grounding(metric_facts, graph_context)
                                if graph_context or metric_facts else "")
        except ValueError as exc:
            return QuestionExecution(generated=GenerateResponse(
                db_id=db_id, question=question, provider=provider, model=model, dialect=dialect,
                strategy=route_question(question), semantic_contract=contract, sql=None,
                accepted=False, termination_reason="context_error", model_error=str(exc)),
                approved_tables=proposed_tables, semantic_contract=contract,
                planner_usage=TokenUsage(),
                routing_ms=round((perf_counter() - routing_started) * 1000),
                planning_ms=0, generation_ms=0)
        routing_ms = round((perf_counter() - routing_started) * 1_000)

        planning_started = perf_counter()
        if progress:
            progress("context_selection" if context_mode == "model1" else "grounding")
        contract = apply_structural_formulas(
            contract, self.glossaries.structural_formulas(db_id, question)
        )
        planner_usage = TokenUsage()
        planner_call_used = False
        # Legacy model1 requests retain wire compatibility, but selection is
        # exclusively retrieval plus deterministic verification for every turn.
        context_mode = "retrieval"

        if graph_enabled and (self.graph_store or (kpi_first and metric_facts)):
            graph_started = perf_counter()
            graph_store = self.graph_store
            snapshot = await budget.wait(asyncio.to_thread(lambda: graph_store.load(
                db_id, identity=graph_identity(schema, catalog, database_profile,
                                               glossary))), kind="work") if graph_store else None
            if snapshot is None and kpi_first and metric_facts:
                snapshot = await budget.wait(asyncio.to_thread(
                    compile_graph, schema, catalog, database_profile, glossary), kind="work")
                if graph_store:
                    await budget.wait(asyncio.to_thread(graph_store.remember, snapshot),
                                      kind="work")
            if snapshot:
                resolution = await budget.wait(asyncio.to_thread(resolve_joins,
                    list(dict.fromkeys([*context_request.tables, *[item.source_relation
                        for item in contract.approved_metrics]])), schema, snapshot=snapshot,
                    compiled_network=(graph_store.network(db_id, identity=snapshot.identity)
                                      if graph_store else None)),
                    kind="work")
                graph_context = {"status": resolution.status,
                    "snapshot_id": snapshot.identity, "catalog_version": snapshot.catalog_version,
                    "explanation": resolution.explanation,
                    "warnings": [item.model_dump(mode="json") for item in resolution.warnings],
                    "relationships": [item.model_dump(mode="json")
                                      for item in resolution.relationships]}
                if kpi_first and metric_facts and contract.kpi_grounding:
                    contract.kpi_grounding.join_status = resolution.status
                    contract.kpi_grounding.required_relationships = [{
                        "child_table": edge.source.removeprefix("table:"),
                        "parent_table": edge.target.removeprefix("table:"),
                        "child_columns": edge.facts["child_columns"],
                        "parent_columns": edge.facts["parent_columns"],
                        "cardinality": edge.facts.get("cardinality", "unknown"),
                        "provenance": edge.provenance,
                    } for edge in resolution.relationships]
                    if resolution.status != "CONNECTED":
                        return QuestionExecution(generated=GenerateResponse(
                            db_id=db_id, question=question, provider=provider, model=model,
                            dialect=dialect, strategy=route_question(question), sql=None,
                            accepted=False, termination_reason="context_error",
                            semantic_contract=contract,
                            model_error=f"KPI_JOIN_{resolution.status}: {resolution.explanation}"),
                            approved_tables=[], semantic_contract=contract,
                            planner_usage=TokenUsage(), routing_ms=routing_ms,
                            planning_ms=round((perf_counter() - planning_started) * 1000),
                            generation_ms=0)
                if resolution.status == "CONNECTED":
                    context_request.tables = resolution.tables
                    for rel in resolution.relationships:
                        if rel.provenance == "approved_catalog":
                            child = rel.source.removeprefix("table:")
                            parent = rel.target.removeprefix("table:")
                            cardinality = rel.facts.get("cardinality", "unknown").upper()
                            catalog_relationships.append(ContextRelationship(
                                left_table=child, right_table=parent,
                                left_column=rel.facts["child_columns"][0],
                                right_column=rel.facts["parent_columns"][0],
                                left_columns=rel.facts["child_columns"],
                                right_columns=rel.facts["parent_columns"],
                                state="APPROVED_CATALOG", join_cardinality=cardinality,
                                recommended_strategy=("UNRESOLVED" if cardinality == "UNKNOWN"
                                                      else "PRE_AGGREGATE"
                                                      if cardinality == "MANY_TO_MANY"
                                                      else "JOIN"),
                                fanout_risk=cardinality not in {"MANY_TO_ONE", "ONE_TO_ONE"}))
                        for table, names in ((rel.source.removeprefix("table:"),
                                              rel.facts.get("child_columns", [])),
                                             (rel.target.removeprefix("table:"),
                                              rel.facts.get("parent_columns", []))):
                            context_request.columns[table] = list(dict.fromkeys([
                                *context_request.columns.get(table, []), *names]))
                    proposed_tables = resolution.tables
                    contract = contract.model_copy(update={"required_columns": list(dict.fromkeys([
                        *contract.required_columns,
                        *[f"{rel.source.removeprefix('table:')}.{column}"
                          for rel in resolution.relationships
                          for column in rel.facts["child_columns"]],
                        *[f"{rel.target.removeprefix('table:')}.{column}"
                          for rel in resolution.relationships
                          for column in rel.facts["parent_columns"]],
                    ]))})
            graph_latency_ms = round((perf_counter() - graph_started) * 1000)
        try:
            semantic_context = (compact_grounding(metric_facts, graph_context)
                                if graph_context or metric_facts else "")
        except ValueError as exc:
            return QuestionExecution(generated=GenerateResponse(
                db_id=db_id, question=question, provider=provider, model=model, dialect=dialect,
                strategy=route_question(question), semantic_contract=contract, sql=None,
                accepted=False, termination_reason="context_error", model_error=str(exc),
                token_usage=planner_usage, telemetry=PipelineTelemetry(
                    planner_call_used=planner_call_used, planner_call=planner_usage,
                    total_model_calls=budget.model_calls,
                    total_database_calls=budget.database_calls)),
                approved_tables=proposed_tables, semantic_contract=contract,
                planner_usage=planner_usage, routing_ms=routing_ms,
                planning_ms=round((perf_counter() - planning_started) * 1000), generation_ms=0)

        approved_concepts = self.glossaries.relevant_concept_ids(db_id, question)
        if progress:
            progress("grounding")
        context_request = verify_context_request(
            context_request,
            schema,
            contract,
            database_profile,
            approved_concepts,
        )
        logger.info("verified_context_request=%s", context_request.model_dump_json())
        contract = reconcile_context_contract(contract, context_request)
        business_context = (
            self.glossaries.retrieve(db_id, question, top_k=5)
            if context_request.business_concepts
            else None
        )
        if value_alias_context:
            business_context = "\n".join(
                item for item in (business_context, value_alias_context) if item
            )
        if graph_context or metric_facts:
            business_context = "\n".join(filter(None, [business_context,
                "Governed semantic grounding: " + semantic_context]))
        historical_examples: list[HistoricalExample] = []
        history_enabled = os.environ.get("TEXT2SQL_HISTORY_ENABLED", "false").casefold() == "true"
        if history_enabled:
            historical_examples = [
                item
                for item in self.history.search(
                    question,
                    db_id,
                    top_k=2,
                    min_score=float(os.environ.get("TEXT2SQL_HISTORY_MIN_SCORE", "0.85")),
                    bm25_pool=20,
                    semantic_pool=5,
                    candidate_tables=set(context_request.tables),
                )
                if validate_sql(item.sql, schema, dialect=dialect).valid
            ][:2]
        if context_request.tables:
            proposed_tables = context_request.tables
        planning_ms = round((perf_counter() - planning_started) * 1_000)

        generation_started = perf_counter()
        if progress:
            progress("generation")
        generation_request = GenerateRequest(
            db_id=db_id,
            question=question,
            evidence="\n".join(
                item for item in (retrieval_evidence, visualization_requirement) if item
            ) or None,
            provider=provider,
            model=model,
            dialect=dialect,
            execute=execute,
            max_rows=max_rows,
            max_attempts=max_attempts,
            approved_tables=proposed_tables,
            semantic_contract=contract,
            business_context=business_context,
            previous_sql=previous_sql,
            optimization_required=optimization_required,
            context_request=context_request,
            planner_call_used=planner_call_used,
            planner_token_usage=planner_usage,
            historical_examples=historical_examples,
            schema_snapshot=VerifiedSchemaSnapshot(
                db_id=db_id, dialect=dialect, live_schema=schema, profile=database_profile,
            ),
        )
        generation_request._catalog_relationships = catalog_relationships
        generation_request._ontology_adapter = ontology
        try:
            generated = await budget.wait(
                self.agent.generate(generation_request, progress=progress, budget=budget),
                kind="work",
            )
        except RequestBudgetExceeded as exc:
            generated = GenerateResponse(
                db_id=db_id,
                question=question,
                provider=provider,
                model=model,
                dialect=dialect,
                strategy=route_question(question),
                semantic_contract=contract,
                sql=None,
                accepted=False,
                termination_reason="model_error",
                model_error=str(exc),
            )
        if (
            generated.accepted
            and execute
            and (
                generated.row_count == 0
                or any(value is None for row in generated.rows for value in row)
            )
        ):
            if progress:
                progress("result_review")
            review_mode = "ZERO_RESULT" if generated.row_count == 0 else "NULL_RESULT"
            review_trace = await RecoveryCoordinator(
                RecoveryTools(database, db_id, schema, database_profile, budget=budget,
                              ontology=ontology)
            ).ainvestigate(
                question=question,
                failed_sql=generated.sql or "",
                failure_code=review_mode,
                failure_message=(
                    "The query executed with zero rows."
                    if review_mode == "ZERO_RESULT"
                    else "The query returned one or more NULL values."
                ),
                allowed_tables=proposed_tables,
                mode=review_mode,
                progress=progress,
                completer=self.agent.model,
                provider=os.environ.get("TEXT2SQL_RECOVERY_PROVIDER") or provider,
                model=os.environ.get("TEXT2SQL_RECOVERY_MODEL") or generation_request.model,
                budget=budget,
            )
            generated = generated.model_copy(update={"recovery": review_trace})
            if (
                review_trace.agent_action == "REPAIR"
                and review_trace.repair_instruction
                and len(generated.attempts) < max_attempts
            ):
                repair_request = generation_request.model_copy(
                    update={
                        "evidence": "\n\n".join(
                            item
                            for item in (
                                retrieval_evidence,
                                recovery_feedback(review_trace),
                            )
                            if item
                        ),
                        "previous_sql": generated.sql,
                        "max_attempts": 1,
                    }
                )
                try:
                    repaired = await budget.wait(
                        self.agent.generate(repair_request, progress=progress, budget=budget),
                        kind="work",
                    )
                except RequestBudgetExceeded:
                    repaired = None
                review_trace = review_trace.model_copy(update={
                    "repair_execution_succeeded": bool(repaired and repaired.accepted),
                    "false_repair": (True if repaired and any(
                        finding.status == "FAIL" for attempt in repaired.attempts
                        for finding in attempt.validation.semantic_findings) else None),
                })
                generated = generated.model_copy(update={"recovery": review_trace})
                if repaired is not None and repaired.accepted:
                    offset = len(generated.attempts)
                    repaired_attempts = [
                        attempt.model_copy(update={"number": offset + index})
                        for index, attempt in enumerate(repaired.attempts, 1)
                        if offset + index <= 3
                    ]
                    repaired = repaired.model_copy(
                        update={
                            "attempts": [*generated.attempts, *repaired_attempts],
                            "token_usage": _add_token_usage(
                                generated.token_usage, repaired.token_usage
                            ),
                            "recovery": review_trace,
                        }
                    )
                    generated = repaired
                else:
                    generated = generated.model_copy(
                        update={
                            "accepted": False,
                            "attempts": [*generated.attempts, *[
                                attempt.model_copy(update={
                                    "number": len(generated.attempts) + index})
                                for index, attempt in enumerate(
                                    repaired.attempts if repaired else [], 1)
                            ]],
                            "token_usage": _add_token_usage(
                                generated.token_usage,
                                repaired.token_usage if repaired else TokenUsage(),
                            ),
                            "execution_status": "NOT_EXECUTED",
                            "termination_reason": "attempt_limit",
                            "rows": [],
                            "columns": [],
                            "row_count": 0,
                        }
                    )
        if correctness_trace is not None and generated.recovery is None:
            generated = generated.model_copy(update={"recovery": correctness_trace})
        generation_ms = round((perf_counter() - generation_started) * 1_000)
        if generated.recovery:
            generated = generated.model_copy(
                update={
                    "token_usage": _add_token_usage(
                        generated.token_usage, generated.recovery.usage.token_usage
                    )
                }
            )
        call_ledger: list[CallLedgerEntry] = [
            CallLedgerEntry(
                component="context_grounding", kind="TOOL", status="SUCCEEDED",
                latency_ms=planning_ms,
            ),
            CallLedgerEntry(
                component="schema_inspection",
                kind="DATABASE",
                status="SUCCEEDED",
                latency_ms=routing_ms,
            )
        ]
        if graph_latency_ms is not None:
            call_ledger.append(CallLedgerEntry(component="semantic_graph_lookup",
                                                kind="TOOL", status="SUCCEEDED",
                                                latency_ms=graph_latency_ms))
        call_ledger.extend(
            CallLedgerEntry(
                component="sql_generation" if index == 0 else "sql_repair",
                kind="MODEL",
                status="SUCCEEDED",
                provider=str(provider),
                requested_model=model,
                effective_model=generation_request.model,
                input_tokens=attempt.token_usage.input_tokens,
                output_tokens=attempt.token_usage.output_tokens,
                latency_ms=attempt.latency_ms,
                retry_count=attempt.token_usage.provider_retries,
            )
            for index, attempt in enumerate(generated.attempts)
        )
        call_ledger.extend(
            CallLedgerEntry(
                component="sql_validation",
                kind="TOOL",
                status="SUCCEEDED",
                latency_ms=attempt.validation_latency_ms,
            )
            for attempt in generated.attempts
            if attempt.validation_latency_ms is not None
        )
        if generated.recovery and generated.recovery.call_ledger:
            call_ledger.extend(generated.recovery.call_ledger)
        elif generated.recovery and generated.recovery.usage.llm_calls:
            call_ledger.extend(
                CallLedgerEntry(
                    component=f"recovery_reasoning_{index + 1}",
                    kind="MODEL",
                    status="UNKNOWN",
                    provider=str(provider),
                    requested_model=model,
                    effective_model=generation_request.model,
                    input_tokens=(
                        generated.recovery.usage.token_usage.input_tokens if index == 0 else None
                    ),
                    output_tokens=(
                        generated.recovery.usage.token_usage.output_tokens if index == 0 else None
                    ),
                )
                for index in range(generated.recovery.usage.llm_calls)
            )
        if execute and generated.attempts:
            call_ledger.extend(
                CallLedgerEntry(
                    component="sql_execution",
                    kind="DATABASE",
                    status=(
                        "FAILED" if attempt.validation.code == "DATABASE_ERROR" else "SUCCEEDED"
                    ),
                    latency_ms=attempt.execution_latency_ms,
                )
                for attempt in generated.attempts
                if attempt.execution_latency_ms is not None
            )
        if generated.recovery and generated.recovery.usage.database_probe_count:
            call_ledger.extend(
                CallLedgerEntry(
                    component=f"recovery_probe_{index + 1}",
                    kind="DATABASE",
                    status="SUCCEEDED",
                )
                for index in range(generated.recovery.usage.database_probe_count)
            )
        generated = generated.model_copy(
            update={
                "telemetry": generated.telemetry.model_copy(
                    update={
                        "model1_latency_ms": 0,
                        "context_grounding_latency_ms": planning_ms,
                        "ab_context_mode": context_mode,
                        "retrieval": retrieval_trace,
                        "selected_table_count": len(context_request.tables),
                        "selected_column_count": sum(
                            len(columns) for columns in context_request.columns.values()
                        ),
                        "metadata_request_count": len(context_request.metadata_requirements),
                        "historical_attempted": history_enabled,
                        "historical_candidates_retrieved": (
                            len(historical_examples) if history_enabled else None
                        ),
                        "historical_examples_admitted": len(historical_examples),
                        "historical_similarity_scores": [
                            item.score for item in historical_examples
                        ],
                        "call_ledger": call_ledger,
                        "total_model_calls": sum(item.kind == "MODEL" for item in call_ledger),
                        "total_database_calls": budget.database_calls
                        + sum(item.component == "sql_execution" for item in call_ledger)
                        + (
                            generated.recovery.usage.database_probe_count
                            if generated.recovery
                            else 0
                        ),
                    }
                )
            }
        )
        return QuestionExecution(
            generated=generated,
            approved_tables=proposed_tables,
            semantic_contract=contract,
            planner_usage=planner_usage,
            routing_ms=routing_ms,
            planning_ms=planning_ms,
            generation_ms=generation_ms,
        )

    def _database(self, db_id: str) -> DatabaseRegistry | PostgresRegistry:
        """Resolve an allowlisted ID without exposing a caller-supplied DSN."""

        if db_id in self.database.list_ids():
            return self.database
        if self.postgres is not None and db_id in self.postgres.configured_ids():
            return self.postgres
        raise DatabaseError("DATABASE_NOT_FOUND", f"Database {db_id} was not found or configured.")


def _add_token_usage(left: TokenUsage, right: TokenUsage) -> TokenUsage:
    return TokenUsage(
        input_tokens=(left.input_tokens or 0) + (right.input_tokens or 0),
        output_tokens=(left.output_tokens or 0) + (right.output_tokens or 0),
        cache_read_tokens=(left.cache_read_tokens or 0) + (right.cache_read_tokens or 0),
        cache_creation_tokens=(left.cache_creation_tokens or 0)
        + (right.cache_creation_tokens or 0),
    )
