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
from typing import Literal, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from pydantic import Field

from semantic_text2sql.database import DatabaseError, DatabaseRegistry
from semantic_text2sql.glossary import GlossaryStore
from semantic_text2sql.hybrid_retrieval import HybridSchemaRetriever
from semantic_text2sql.models import ChatRequest, ChatResponse, StrictModel
from semantic_text2sql.postgres import PostgresRegistry
from semantic_text2sql.profiling import ProfileStore
from semantic_text2sql.runtime import RequestBudget

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


class MetadataToolOutput(StrictModel):
    tool: Literal["search_schema", "get_business_context"]
    evidence_id: str
    facts: list[str] = Field(default_factory=list, max_length=30)
    found: bool


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
    if _BUSINESS_QUESTION.search(text):
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
    ) -> None:
        self.database = database
        self.postgres = postgres
        self.profiles = profiles
        self.glossaries = glossaries
        self.retriever = retriever
        self.sql_handler = sql_handler
        graph = StateGraph(_GraphState)
        graph.add_node("route", self._route_node)
        graph.add_node("schema", self._schema_node)
        graph.add_node("business", self._business_node)
        graph.add_node("sql", self._sql_node)
        graph.add_edge(START, "route")
        graph.add_conditional_edges("route", lambda state: state["route"], {
            "schema": "schema", "business": "business", "sql": "sql",
        })
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
                "\n".join(result.facts)
                if result.facts
                else "No verified information was found."
            ),
            provenance=[result.evidence_id],
        )

    def _registry(self, db_id: str) -> DatabaseRegistry | PostgresRegistry:
        if db_id in self.database.list_ids():
            return self.database
        if self.postgres is not None and db_id in self.postgres.configured_ids():
            return self.postgres
        raise DatabaseError("DATABASE_NOT_FOUND", "The selected database is not configured.")

    async def search_schema(self, tool_input: MetadataToolInput) -> MetadataToolOutput:
        """Read allowlisted schema; use the existing retriever for column questions."""
        budget = RequestBudget(timeout_seconds=10, max_model_calls=0, max_database_calls=1)
        schema = await budget.wait(
            asyncio.to_thread(self._registry(tool_input.db_id).inspect, tool_input.db_id),
            kind="database",
        )
        question = tool_input.question
        facts: list[str]
        if re.search(r"\b(?:columns?|relationships?|contain|related)\b", question, re.I):
            profile = self.profiles.load(schema.dialect, tool_input.db_id)
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
        if not facts:
            facts = ["No approved business definition matches this question."]
        return MetadataToolOutput(
            tool="get_business_context",
            evidence_id=_evidence_id("get_business_context", tool_input.db_id, facts),
            facts=facts,
            found=not facts[0].startswith("No approved"),
        )
