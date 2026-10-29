"""Opt-in V6 conversation bridge over the existing governed chat pipeline.

The graph handles only high-confidence metadata questions. Every analytical
question delegates to the existing chat handler; this module never generates
or executes SQL and never opens a database connection directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from pydantic import Field

from semantic_text2sql.database import DatabaseError, DatabaseRegistry
from semantic_text2sql.glossary import GlossaryStore
from semantic_text2sql.hybrid_retrieval import HybridSchemaRetriever
from semantic_text2sql.models import ChatRequest, ChatResponse, StrictModel, VerifiedSchemaSnapshot
from semantic_text2sql.postgres import PostgresRegistry
from semantic_text2sql.profiling import ProfileStore
from semantic_text2sql.runtime import RequestBudget
from semantic_text2sql.semantic_catalog import SemanticCatalogStore

Route = Literal["schema", "business", "sql"]
SqlHandler = Callable[[ChatRequest], Awaitable[ChatResponse]]
_SCHEMA_QUESTION = re.compile(
    r"^(?:what|which|list|show|describe|find)\b.*\b(?:tables?|columns?|schema|relationships?)\b",
    re.I,
)
_BUSINESS_QUESTION = re.compile(
    r"^(?:what does|define|what is the (?:meaning|definition) of|explain the definition of)\b",
    re.I,
)
_METRICS_QUESTION = re.compile(r"^(?:what|which|list|show)\b.*\b(?:metrics?|kpis?)\b", re.I)
_ANALYTICAL_ACTION = re.compile(
    r"\b(?:count|sum|average|total|trend|compare|rank|top|bottom|plot|chart|filter)\b",
    re.I,
)


class MetadataToolInput(StrictModel):
    db_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    question: str = Field(min_length=1, max_length=4_000)


class BusinessValueCandidate(StrictModel):
    table: str
    column: str
    value: str
    match: Literal["EXACT", "FUZZY", "APPROVED_ALIAS"]
    provenance: Literal["PROFILE_COMPLETE", "PROFILE_SAMPLE"]
    ambiguous: bool = False
    verified_live: bool = False
    profile_freshness: str | None = None
    ambiguity_reason: str | None = None


class MetadataToolOutput(StrictModel):
    tool: Literal[
        "list_tables",
        "search_schema",
        "describe_table",
        "get_relationships",
        "get_column_profile",
        "get_business_context",
        "search_business_context",
    ]
    evidence_id: str
    facts: list[str] = Field(default_factory=list, max_length=30)
    found: bool
    value_candidates: list[BusinessValueCandidate] = Field(default_factory=list)


@dataclass(frozen=True)
class BridgeOutcome:
    route: Route
    response: ChatResponse


class _GraphState(TypedDict, total=False):
    request: ChatRequest
    route: Route
    response: ChatResponse


def _route(message: str) -> Route:
    text = " ".join(message.split())
    if _BUSINESS_QUESTION.search(text) and not re.search(
        r"\b(?:look like|over time|by month|by year|trend|compare|change|how much|how many)\b",
        text,
        re.I,
    ):
        return "business"
    if _SCHEMA_QUESTION.search(text) and not _ANALYTICAL_ACTION.search(text):
        return "schema"
    if _METRICS_QUESTION.search(text) and not _ANALYTICAL_ACTION.search(text):
        return "business"
    return "sql"


def _evidence_id(tool: str, db_id: str, facts: list[str]) -> str:
    digest = hashlib.sha256("\n".join([tool, db_id, *facts]).encode()).hexdigest()[:16]
    return f"{tool}-{digest}"


class ConversationalAnalyticsBridge:
    """Bounded graph adapter; existing chat remains the only SQL path."""

    def __init__(
        self,
        *,
        database: DatabaseRegistry,
        postgres: PostgresRegistry | None,
        profiles: ProfileStore,
        glossaries: GlossaryStore,
        retriever: HybridSchemaRetriever,
        sql_handler: SqlHandler,
        semantic_catalogs: SemanticCatalogStore | None = None,
    ) -> None:
        self.database = database
        self.postgres = postgres
        self.profiles = profiles
        self.glossaries = glossaries
        self.semantic_catalogs = semantic_catalogs
        self._categorical_index: dict[tuple[str, str, str], dict[tuple[str, str], list[str]]] = {}
        self.retriever = retriever
        self.sql_handler = sql_handler
        graph = StateGraph(_GraphState)
        graph.add_node("route", self._route_node)
        graph.add_node("schema", self._schema_node)
        graph.add_node("business", self._business_node)
        graph.add_node("sql", self._sql_node)
        graph.add_edge(START, "route")
        graph.add_conditional_edges(
            "route",
            lambda state: state["route"],
            {
                "schema": "schema",
                "business": "business",
                "sql": "sql",
            },
        )
        for name in ("schema", "business", "sql"):
            graph.add_edge(name, END)
        self.graph = graph.compile()

    async def run(self, request: ChatRequest) -> BridgeOutcome:
        result = await self.graph.ainvoke({"request": request})
        return BridgeOutcome(route=cast(Route, result["route"]), response=result["response"])

    def _route_node(self, state: _GraphState) -> _GraphState:
        return {"route": _route(state["request"].message)}

    async def _schema_node(self, state: _GraphState) -> _GraphState:
        request = state["request"]
        result = await self.search_schema(
            MetadataToolInput(db_id=request.db_id, question=request.message)
        )
        return {"response": self._tool_response(request, result)}

    async def _business_node(self, state: _GraphState) -> _GraphState:
        request = state["request"]
        result = self.get_business_context(
            MetadataToolInput(db_id=request.db_id, question=request.message)
        )
        return {"response": self._tool_response(request, result)}

    async def _sql_node(self, state: _GraphState) -> _GraphState:
        return {"response": await self.sql_handler(state["request"])}

    def _tool_response(self, request: ChatRequest, result: MetadataToolOutput) -> ChatResponse:
        return ChatResponse(
            session_id=request.session_id,
            operation="EXPLAIN_CONTEXT",
            resolved_question=request.message,
            message=(
                "\n".join(result.facts) if result.facts else "No verified information was found."
            ),
            provenance=[result.evidence_id],
        )

    def _registry(self, db_id: str) -> DatabaseRegistry | PostgresRegistry:
        if db_id in self.database.list_ids():
            return self.database
        if self.postgres is not None and db_id in self.postgres.configured_ids():
            return self.postgres
        raise DatabaseError("DATABASE_NOT_FOUND", "The selected database is not configured.")

    async def inspect_metadata(
        self,
        db_id: str,
        tool: Literal[
            "list_tables",
            "search_schema",
            "describe_table",
            "get_relationships",
            "get_column_profile",
        ],
        target: str = "",
        *,
        snapshot: VerifiedSchemaSnapshot | None = None,
    ) -> MetadataToolOutput:
        """Return facts for the requested metadata capability, not a generic search."""
        verified = snapshot or await self.load_metadata_snapshot(db_id)
        if verified.db_id != db_id:
            raise ValueError("The metadata snapshot belongs to another database.")
        schema = verified.live_schema
        profile = verified.profile
        tokens = {part.casefold() for part in re.findall(r"[A-Za-z_][A-Za-z_0-9]*", target)}
        selected = [item for item in schema.tables if item.name.casefold() in tokens]
        if tool == "list_tables":
            facts = [f"{item.name}: {len(item.columns)} columns" for item in schema.tables[:30]]
        elif tool == "describe_table":
            facts = [
                f"{item.name}: "
                + ", ".join(
                    f"{column.name} ({column.data_type}, "
                    f"{'primary key' if column.primary_key else 'ordinary column'})"
                    for column in item.columns
                )
                for item in selected[:3]
            ]
        elif tool == "get_relationships":
            facts = [
                f"{link.from_table}.{link.from_column} → {link.to_table}.{link.to_column}"
                for link in schema.relationships
                if not selected
                or link.from_table in {item.name for item in selected}
                or link.to_table in {item.name for item in selected}
            ][:30]
        elif tool == "get_column_profile":
            facts = []
            if profile is not None:
                for column in profile.columns:
                    if column.column.casefold() not in tokens:
                        continue
                    if selected and column.table not in {item.name for item in selected}:
                        continue
                    facts.append(
                        f"{column.table}.{column.column}: type={column.database_type}; "
                        f"semantic_type={column.semantic_type}; "
                        f"observed_format={column.observed_format or 'unknown'}; "
                        f"observed_nulls={column.null_count > 0}"
                    )
                    if len(facts) >= 8:
                        break
        elif tool == "search_schema":
            return await self.search_schema(
                MetadataToolInput(db_id=db_id, question=target), snapshot=verified
            )
        else:
            raise ValueError(f"Unsupported metadata tool: {tool}")
        if not facts:
            facts = ["No matching verified metadata was found."]
        return MetadataToolOutput(
            tool=tool,
            evidence_id=_evidence_id(tool, db_id, facts),
            facts=facts,
            found=not facts[0].startswith("No matching"),
        )

    async def load_metadata_snapshot(self, db_id: str) -> VerifiedSchemaSnapshot:
        """Inspect an allowlisted database once for one assistant turn."""
        schema = await asyncio.to_thread(self._registry(db_id).inspect, db_id)
        return VerifiedSchemaSnapshot(
            db_id=db_id,
            dialect=schema.dialect,
            live_schema=schema,
            profile=self.profiles.load(schema.dialect, db_id),
        )

    async def search_schema(
        self,
        tool_input: MetadataToolInput,
        *,
        snapshot: VerifiedSchemaSnapshot | None = None,
    ) -> MetadataToolOutput:
        """Read allowlisted schema; use the existing retriever for column questions."""
        budget = RequestBudget(timeout_seconds=10, max_model_calls=0, max_database_calls=1)
        verified: VerifiedSchemaSnapshot
        if snapshot is None:
            verified = await budget.wait(
                self.load_metadata_snapshot(tool_input.db_id), kind="database"
            )
        else:
            verified = snapshot
        if verified.db_id != tool_input.db_id:
            raise ValueError("The metadata snapshot belongs to another database.")
        schema = verified.live_schema
        question = tool_input.question
        facts: list[str]
        if re.search(r"\b(?:columns?|relationships?|contain|related)\b", question, re.I):
            profile = verified.profile
            glossary = self.glossaries.load(tool_input.db_id)
            _, selection, _ = await budget.wait(
                asyncio.to_thread(
                    self.retriever.retrieve, question, None, schema, profile, glossary
                ),
                kind="work",
            )
            facts = [
                f"{table}: {', '.join(selection.columns.get(table, []))}"
                for table in selection.tables[:5]
                if selection.columns.get(table)
            ]
            if "relationship" in question.casefold():
                selected = set(selection.tables)
                facts.extend(
                    f"Verified relationship: {link.from_table}.{link.from_column} → "
                    f"{link.to_table}.{link.to_column}"
                    for link in schema.relationships
                    if link.from_table in selected and link.to_table in selected
                )
        else:
            facts = [f"{table.name} ({len(table.columns)} columns)" for table in schema.tables[:30]]
        if not facts:
            facts = ["No matching schema information was found in the configured database."]
        return MetadataToolOutput(
            tool="search_schema",
            evidence_id=_evidence_id("search_schema", tool_input.db_id, facts),
            facts=facts,
            found=not facts[0].startswith("No matching"),
        )

    def get_business_context(self, tool_input: MetadataToolInput) -> MetadataToolOutput:
        """Return only curated definitions; unknown metrics remain unknown."""
        self._registry(tool_input.db_id)
        glossary = self.glossaries.load(tool_input.db_id)
        facts: list[str] = []
        if glossary is not None:
            question = tool_input.question.casefold()
            listing_metrics = bool(_METRICS_QUESTION.search(tool_input.question))
            for term in glossary.terms:
                names = [term.term, *term.synonyms]
                if (listing_metrics and term.formula) or any(
                    re.search(rf"(?<!\w){re.escape(name.casefold())}(?!\w)", question)
                    for name in names
                ):
                    formula = f" Formula: {term.formula}." if term.formula else ""
                    facts.append(f"{term.term}: {term.definition}{formula}")
                if len(facts) == 8:
                    break
        catalog = self.semantic_catalogs.load(tool_input.db_id) if self.semantic_catalogs else None
        if catalog is not None and len(facts) < 8:
            question = tool_input.question.casefold()
            listing_metrics = bool(_METRICS_QUESTION.search(tool_input.question))
            for metric in catalog.metrics:
                if metric.status != "approved":
                    continue
                if listing_metrics or any(
                    re.search(rf"(?<!\w){re.escape(name.casefold())}(?!\w)", question)
                    for name in (metric.id, metric.label)
                ):
                    fact = (
                        f"{metric.label} ({metric.id}): {metric.description} "
                        f"Formula: {metric.formula}. Source: {metric.source_relation}."
                    )
                    if fact not in facts:
                        facts.append(fact)
                if len(facts) == 8:
                    break
        if not facts:
            facts = ["No approved business definition matches this question."]
        return MetadataToolOutput(
            tool="get_business_context",
            evidence_id=_evidence_id("get_business_context", tool_input.db_id, facts),
            facts=facts,
            found=not facts[0].startswith("No approved"),
        )

    def search_business_context(self, tool_input: MetadataToolInput) -> MetadataToolOutput:
        """Combine approved definitions with bounded, advisory profiled value matches.

        A profile can be stale or sampled. Candidates are never reported as live
        existence proofs and this tool does not execute DISTINCT scans.
        """
        registry = self._registry(tool_input.db_id)
        definitions = self.get_business_context(tool_input)
        profile = self.profiles.load(
            "postgres" if isinstance(registry, PostgresRegistry) else "sqlite",
            tool_input.db_id,
        )
        aliases = self.glossaries.matched_value_aliases(
            tool_input.db_id, tool_input.question
        )
        question = tool_input.question.casefold()
        words = {word.casefold() for word in re.findall(r"[\w-]+", question)}
        candidates: list[BusinessValueCandidate] = []
        index: dict[tuple[str, str], list[str]] = {}
        if profile is not None:
            index_key = (
                tool_input.db_id,
                "postgres" if isinstance(registry, PostgresRegistry) else "sqlite",
                profile.profiled_at,
            )
            index = self._categorical_index.setdefault(index_key, {})
            for column in profile.columns:
                if column.semantic_type not in {"categorical", "text"}:
                    continue
                values = index.setdefault((column.table, column.column), list(
                    column.allowed_values[:50]
                    if column.exact and column.allowed_values
                    else [item.value for item in column.top_values[:10]]
                ))
                provenance: Literal["PROFILE_COMPLETE", "PROFILE_SAMPLE"] = (
                    "PROFILE_COMPLETE"
                    if column.exact and column.allowed_values else "PROFILE_SAMPLE"
                )
                for value in values:
                    if not value or len(value) > 100:
                        continue
                    match: Literal["EXACT", "FUZZY", "APPROVED_ALIAS"] | None = None
                    if re.search(rf"(?<!\w){re.escape(value.casefold())}(?!\w)", question):
                        match = "EXACT"
                    elif any(
                        canonical.casefold() == value.casefold()
                        for canonical in aliases.values()
                    ):
                        match = "APPROVED_ALIAS"
                    elif len(value) >= 3 and any(
                        len(word) >= 3
                        and SequenceMatcher(None, word, value.casefold()).ratio() >= 0.82
                        for word in words
                    ):
                        match = "FUZZY"
                    if match is not None:
                        candidates.append(BusinessValueCandidate(
                            table=column.table,
                            column=column.column,
                            value=value,
                            match=match,
                            provenance=provenance,
                            ambiguous=match == "FUZZY",
                            profile_freshness=profile.profiled_at,
                            ambiguity_reason=(
                                "Fuzzy matches require live verification."
                                if match == "FUZZY" else None
                            ),
                        ))
        candidates.sort(key=lambda item: (
            {"EXACT": 0, "APPROVED_ALIAS": 1, "FUZZY": 2}[item.match],
            item.table, item.column, item.value,
        ))
        candidates = candidates[:5]
        facts = definitions.facts if definitions.found else []
        if candidates:
            facts = [*facts, "Value matches are profiled candidates, not live existence proof."]
        if not facts:
            facts = ["No approved definition or profiled value matches this question."]
        return MetadataToolOutput(
            tool="search_business_context",
            evidence_id=_evidence_id("search_business_context", tool_input.db_id, facts),
            facts=facts,
            found=definitions.found or bool(candidates),
            value_candidates=candidates,
        )
