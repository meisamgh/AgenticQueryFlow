"""Local-workspace-only, metadata-only semantic exploration HTTP boundary."""
from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from threading import RLock
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response

from semantic_text2sql.dashboard_studio import dashboard_principal
from semantic_text2sql.database import DatabaseRegistry
from semantic_text2sql.glossary import GlossaryStore
from semantic_text2sql.join_resolver import resolve_joins
from semantic_text2sql.postgres import PostgresRegistry
from semantic_text2sql.profiling import ProfileStore
from semantic_text2sql.semantic_catalog import SemanticCatalogStore, schema_fingerprint
from semantic_text2sql.semantic_explorer import (
    EDGE_KINDS,
    NODE_KINDS,
    Direction,
    ExplorerCache,
    ExplorerGraph,
    ExplorerIndex,
    ExplorerView,
    display_edge,
    display_node,
)
from semantic_text2sql.semantic_graph import SemanticGraphStore, compile_graph, graph_identity

EXPLORER_DB_ID = "debit_card_specializing"


def semantic_explorer_router(
    database: Callable[[str], DatabaseRegistry | PostgresRegistry],
    sources: Callable[[], list[str]], profiles: ProfileStore, catalogs: SemanticCatalogStore,
    glossaries: GlossaryStore, store: SemanticGraphStore,
) -> APIRouter:
    router = APIRouter(prefix="/api/semantic-graph", tags=["Semantic Explorer"])
    cache, lock = ExplorerCache(), RLock()

    def authorize(request: Request, response: Response) -> None:
        principal = dashboard_principal(request)
        if principal.authenticated:
            raise HTTPException(403, "Remote exploration requires datasource-level authorization.")
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Vary"] = "Cookie, Authorization"
        if (os.environ.get("TEXT2SQL_SEMANTIC_EXPLORER_ENABLED", "true").lower() != "true"
                or os.environ.get("TEXT2SQL_GRAPH_ENABLED", "true").lower() != "true"):
            raise HTTPException(503, "Semantic exploration is disabled in this workspace.")

    def load(db_id: str) -> tuple[ExplorerIndex, list[str], Any]:
        if db_id != EXPLORER_DB_ID:
            raise HTTPException(
                404, "Semantic Explorer is available only for the debit-card database.")
        with lock:
            registry = database(db_id)  # Existing configured datasource allowlist.
            schema = registry.inspect(db_id)  # Metadata only; existing mtime cache.
            if sum(len(table.columns) for table in schema.tables) > 20000:
                raise HTTPException(413, "Schema exceeds the explorer metadata limit.")
            catalog = catalogs.load(db_id)
            warnings = []
            if catalog and (catalog.metadata_status in {"stale", "conflicting"}
                            or len(catalog.metrics) > 256
                            or catalog.schema_fingerprint and
                            catalog.schema_fingerprint != schema_fingerprint(schema)):
                catalog = None
                warnings.append("Approved KPI metadata is stale/unavailable; showing schema only.")
            profile = profiles.load(schema.dialect, db_id)
            glossary = glossaries.load(db_id)
            identity = graph_identity(schema, catalog, profile, glossary)
            cached = cache.compatible(identity)
            if cached is not None:
                return cached, warnings, schema
            snapshot = store.load(db_id, identity=identity)
            if snapshot is None:
                snapshot = compile_graph(schema, catalog, profile, glossary)
                # Explorer GETs do not write snapshots. Startup/CLI retain
                # ownership of persistence; this cache is only an in-memory view.
            network = store.network(db_id, identity=identity)
            if network is None:
                network = snapshot.network()
            return cache.get(snapshot, network), warnings, schema

    async def current(db_id: str) -> tuple[ExplorerIndex, list[str], Any]:
        try:
            return await asyncio.wait_for(asyncio.to_thread(load, db_id), timeout=10)
        except TimeoutError as exc:
            raise HTTPException(503, "Metadata inspection timed out. Try again later.") from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(503, "Compatible semantic metadata is unavailable.") from exc

    def filters(values: list[str] | None, allowed: tuple[str, ...]) -> set[str] | None:
        if values is not None and (len(values) > len(allowed) or not set(values) <= set(allowed)):
            raise HTTPException(422, "Unsupported graph filter.")
        return set(values) if values is not None else None

    @router.get("/sources")
    def datasource_list(request: Request, response: Response) -> list[str]:
        authorize(request, response)
        return [db_id for db_id in sources() if db_id == EXPLORER_DB_ID]

    @router.get("", response_model=ExplorerGraph)
    @router.get("/neighborhood", response_model=ExplorerGraph)
    @router.get("/lineage", response_model=ExplorerGraph)
    async def graph(
        request: Request, response: Response, db_id: Annotated[str, Query(max_length=128)],
        view: ExplorerView = "schema", node_id: Annotated[str | None, Query(max_length=512)] = None,
        depth: Annotated[int, Query(ge=0, le=6)] = 1,
        node_limit: Annotated[int, Query(ge=1, le=250)] = 60,
        edge_limit: Annotated[int, Query(ge=1, le=500)] = 160,
        kinds: Annotated[list[str] | None, Query()] = None,
        relations: Annotated[list[str] | None, Query()] = None,
        direction: Direction = "both", search: Annotated[str, Query(max_length=160)] = "",
    ) -> ExplorerGraph:
        authorize(request, response)
        node_types, edge_types = filters(kinds, NODE_KINDS), filters(relations, EDGE_KINDS)
        lineage = request.url.path.endswith("/lineage")
        if lineage and (node_id is None or direction == "both"):
            raise HTTPException(422, "Lineage needs a node and upstream/downstream direction.")
        index, warnings, _ = await current(db_id)
        try:
            result = index.graph(view=view, node_id=node_id, depth=depth, node_limit=node_limit,
                                 edge_limit=edge_limit, kinds=node_types, relations=edge_types,
                                 direction=direction, lineage=lineage)
        except KeyError as exc:
            raise HTTPException(404, "Node not available in this datasource/view.") from exc
        if search.strip():
            visible = index.visible(view, node_types if node_types is not None else set(NODE_KINDS))
            matches = [key for key in sorted(visible) if search.casefold() in
                       (key + " " + display_node(index.nodes[key]).label).casefold()]
            selected = set(matches[:node_limit])
            result.nodes = [display_node(index.nodes[key]) for key in sorted(selected)]
            edges = [edge for edge in index.edges.values() if edge.source in selected
                     and edge.target in selected
                     and (edge_types is None or edge.kind in edge_types)]
            result.edges = [display_edge(edge) for edge in sorted(edges, key=lambda edge: edge.id)
                            [:edge_limit]]
            result.total_nodes, result.total_edges = len(matches), len(edges)
            result.truncated = len(matches) > node_limit or len(edges) > edge_limit
        result.warnings.extend(warnings)
        return result

    @router.get("/nodes/{node_id:path}")
    async def details(
        request: Request, response: Response, db_id: str, node_id: str,
    ) -> dict[str, Any]:
        authorize(request, response)
        index, warnings, _ = await current(db_id)
        node = index.nodes.get(node_id)
        if node is None:
            raise HTTPException(404, "Node not available in this datasource.")
        metadata: dict[str, Any] = {}
        if node.kind == "TABLE":
            columns = sorted((item for item in index.nodes.values() if item.kind == "COLUMN"
                              and item.facts.get("table") == node.facts["relation"]),
                             key=lambda item: item.id)
            metadata["columns"] = [display_node(item).model_dump() for item in columns[:100]]
            metadata["column_count"] = len(columns)
            metadata["columns_truncated"] = len(columns) > 100
            metadata["associated_kpis"] = [display_node(item).model_dump() for item in
                index.nodes.values() if item.kind == "METRIC"
                and item.facts.get("source_relation") == node.facts["relation"]][:32]
        edges = sorted((edge for edge in index.edges.values() if node_id in
                        {edge.source, edge.target}), key=lambda item: item.id)
        return {"db_id": db_id, "node": display_node(node, detail=True), **metadata,
                "relationships": [display_edge(edge) for edge in edges[:100]],
                "relationships_truncated": len(edges) > 100, "warnings": warnings,
                "catalog_version": index.snapshot.catalog_version}

    @router.get("/connect")
    async def connectivity(
        request: Request, response: Response, db_id: str,
        tables: Annotated[list[str], Query(min_length=2, max_length=8)],
    ) -> dict[str, Any]:
        authorize(request, response)
        index, warnings, schema = await current(db_id)
        result = resolve_joins(tables, schema, snapshot=index.snapshot,
                               compiled_network=index.network)
        return {"db_id": db_id, "status": result.status, "tables": result.tables,
                "nodes": [display_node(index.nodes[f"table:{name}"]) for name in result.tables
                          if f"table:{name}" in index.nodes],
                "bridges": result.bridges, "explanation": result.explanation,
                "edges": [display_edge(edge) for edge in result.relationships],
                "warnings": [item.model_dump() for item in result.warnings],
                "metadata_warnings": warnings}

    return router
