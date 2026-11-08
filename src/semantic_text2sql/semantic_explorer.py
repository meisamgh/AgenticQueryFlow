"""Bounded display/lineage views over the existing metadata NetworkX snapshot.

No business-row queries, SQL generation, inference, graph mutation or new registry.
Only explicitly selected public metadata is serialized.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
from threading import RLock
from typing import Any, Literal

import networkx as nx
from pydantic import Field

from semantic_text2sql.models import StrictModel
from semantic_text2sql.semantic_graph import GraphEdge, GraphNode, GraphSnapshot

type ExplorerView = Literal["schema", "kpi", "combined"]
type Direction = Literal["upstream", "downstream", "both"]
NODE_KINDS = ("TABLE", "COLUMN", "METRIC", "DIMENSION", "BUSINESS_CONCEPT", "BUSINESS_RULE")
EDGE_KINDS = ("REFERENCES", "DEPENDS_ON", "USES_COLUMN", "GROUPABLE_BY", "FILTERED_BY",
              "HAS_COLUMN", "TIME_BY")


class ExplorerNode(StrictModel):
    id: str
    kind: str
    label: str
    status: Literal["observed", "approved", "suggested"]
    provenance: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExplorerEdge(StrictModel):
    id: str
    source: str
    target: str
    kind: str
    provenance: str
    authoritative: bool
    status: Literal["authoritative", "definition", "suggested", "forbidden"]
    cardinality: str | None = None
    fanout_risk: bool | None = None
    child_columns: list[str] = Field(default_factory=list)
    parent_columns: list[str] = Field(default_factory=list)


class ExplorerGraph(StrictModel):
    db_id: str
    view: ExplorerView
    nodes: list[ExplorerNode]
    edges: list[ExplorerEdge]
    total_nodes: int
    total_edges: int
    truncated: bool
    warnings: list[str] = Field(default_factory=list)
    distances: dict[str, int] = Field(default_factory=dict)
    catalog_version: int | None = None
    potential_impact: bool = False


def display_node(node: GraphNode, *, detail: bool = False) -> ExplorerNode:
    facts = node.facts
    keys = {"TABLE": ("relation", "description", "primary_key", "grain"),
            "COLUMN": ("table", "column", "type", "primary_key", "nullable"),
            "METRIC": ("id", "label", "unit", "version", "source_relation", "catalog_version"),
            "DIMENSION": ("table", "column"), "BUSINESS_RULE": ("predicate",),
            "BUSINESS_CONCEPT": ("term", "definition", "version")}[node.kind]
    if detail and node.kind == "METRIC":
        keys = (*keys, "description", "formula", "required_fields", "dependencies",
                "mandatory_filters", "dimensions", "valid_time_grains", "aggregation", "rollup")
    metadata = {key: facts[key] for key in keys if key in facts}
    # No arbitrary fact serialization: approval identities/notes, profile values,
    # graph identities/checksums, paths and cache internals never enter this DTO.
    label = str(facts.get("label") or facts.get("relation") or facts.get("column")
                or facts.get("term") or ("Mandatory filter" if node.kind == "BUSINESS_RULE"
                                         else node.id.split(":", 1)[-1]))
    approved = node.kind in {"METRIC", "BUSINESS_RULE"}
    return ExplorerNode(id=node.id, kind=node.kind, label=label,
                        status="approved" if approved else "observed" if node.kind
                        in {"TABLE", "COLUMN"} else "suggested",
                        provenance="approved_catalog" if approved else "live_schema" if
                        node.kind in {"TABLE", "COLUMN"} else "metadata_suggestion",
                        metadata=metadata)


def display_edge(edge: GraphEdge) -> ExplorerEdge:
    forbidden = bool(edge.facts.get("forbidden"))
    cardinality = edge.facts.get("cardinality") if edge.kind == "REFERENCES" else None
    return ExplorerEdge(
        id=edge.id, source=edge.source, target=edge.target, kind=edge.kind,
        provenance=edge.provenance, authoritative=edge.authoritative,
        status="forbidden" if forbidden else "authoritative" if edge.authoritative else
               "definition" if edge.kind in {"GROUPABLE_BY", "FILTERED_BY"} else "suggested",
        cardinality=cardinality,
        fanout_risk=bool(edge.facts.get("fanout_risk", cardinality not in
                                       {"many_to_one", "one_to_one"}))
        if edge.kind == "REFERENCES" else None,
        child_columns=list(edge.facts.get("child_columns", [])),
        parent_columns=list(edge.facts.get("parent_columns", [])),
    )


@dataclass
class ExplorerIndex:
    snapshot: GraphSnapshot
    network: nx.MultiDiGraph[str, dict[str, Any], dict[str, Any]]
    nodes: dict[str, GraphNode]
    edges: dict[str, GraphEdge]

    def visible(self, view: ExplorerView, kinds: set[str]) -> set[str]:
        return {key for key, node in self.nodes.items() if node.kind in kinds and
                (view != "schema" or node.kind in {"TABLE", "COLUMN"}) and
                (view != "kpi" or node.kind != "BUSINESS_CONCEPT")}

    def graph(
        self, *, view: ExplorerView = "schema", node_id: str | None = None, depth: int = 0,
        node_limit: int = 60, edge_limit: int = 160, kinds: set[str] | None = None,
        relations: set[str] | None = None, direction: Direction = "both", lineage: bool = False,
    ) -> ExplorerGraph:
        visible = self.visible(view, kinds if kinds is not None else set(NODE_KINDS))
        allowed_edges = relations if relations is not None else set(EDGE_KINDS)
        if node_id is not None and node_id not in visible:
            raise KeyError("Node not available in the selected datasource/view.")
        if node_id:
            seeds = [node_id]
        else:
            initial = "TABLE" if view == "schema" else "METRIC" if view == "kpi" else None
            seeds = sorted(key for key in visible if self.nodes[key].kind == initial or
                           initial is None and self.nodes[key].kind != "COLUMN")[:node_limit]
        distances = dict.fromkeys(seeds, 0)
        selected = set(seeds)
        queue = deque(seeds)
        visited_edges: set[str] = set()
        examined = 0
        truncated = len(seeds) < len(visible) if node_id is None else False
        while queue:
            current = queue.popleft()
            if distances[current] >= depth:
                continue
            links = []
            for source, target, key in [*self.network.out_edges(current, keys=True),
                                         *self.network.in_edges(current, keys=True)]:
                edge = self.edges.get(key)
                if edge is None or edge.kind not in allowed_edges:
                    continue
                neighbor = target if source == current else source
                if neighbor not in visible:
                    continue
                if lineage and not self._lineage_step(edge, current, direction):
                    continue
                if not lineage and direction != "both" and (
                    direction == "upstream" and source != current or
                    direction == "downstream" and target != current
                ):
                    continue
                links.append((key, neighbor))
            for key, neighbor in sorted(set(links)):
                examined += 1
                if examined > 5000 or len(visited_edges) >= edge_limit:
                    truncated = True
                    queue.clear()
                    break
                if neighbor not in selected:
                    if len(selected) >= node_limit:
                        truncated = True
                        continue
                    selected.add(neighbor)
                    distances[neighbor] = distances[current] + 1
                    queue.append(neighbor)
                visited_edges.add(key)
        if not lineage:
            # Initial pages include only edges whose endpoints are already sent.
            all_edges = [key for key, edge in self.edges.items() if edge.source in selected
                         and edge.target in selected and edge.kind in allowed_edges]
        else:
            all_edges = sorted(visited_edges)
        if len(all_edges) > edge_limit:
            truncated = True
        return ExplorerGraph(
            db_id=self.snapshot.db_id, view=view,
            nodes=[display_node(self.nodes[key]) for key in sorted(selected)],
            edges=[display_edge(self.edges[key]) for key in sorted(all_edges)[:edge_limit]],
            total_nodes=len(visible), total_edges=sum(
                edge.source in visible and edge.target in visible and edge.kind in allowed_edges
                for edge in self.edges.values()),
            truncated=truncated, distances=distances if node_id else {},
            catalog_version=self.snapshot.catalog_version,
            potential_impact=lineage and direction == "downstream",
        )

    def _lineage_step(self, edge: GraphEdge, current: str, direction: Direction) -> bool:
        if edge.kind == "HAS_COLUMN":
            return direction == "upstream" and edge.target == current
        if edge.kind not in {"DEPENDS_ON", "USES_COLUMN", "FILTERED_BY", "GROUPABLE_BY"}:
            return False
        source = self.nodes[edge.source]
        valid = (edge.authoritative or source.kind == "METRIC" and
                 source.facts.get("authoritative") is True and
                 edge.kind in {"FILTERED_BY", "GROUPABLE_BY"})
        if not valid or direction == "upstream" and edge.kind == "GROUPABLE_BY":
            return False
        return edge.source == current if direction == "upstream" else edge.target == current


class ExplorerCache:
    """Small DTO index cache over the existing store's compatible network object."""
    def __init__(self) -> None:
        self._entries: OrderedDict[str, ExplorerIndex] = OrderedDict()
        self._lock = RLock()

    def compatible(self, identity: str) -> ExplorerIndex | None:
        with self._lock:
            return next((entry for entry in reversed(self._entries.values())
                         if entry.snapshot.identity == identity), None)

    def get(
        self, snapshot: GraphSnapshot,
        network: nx.MultiDiGraph[str, dict[str, Any], dict[str, Any]],
    ) -> ExplorerIndex:
        with self._lock:
            cached = self._entries.get(snapshot.content_checksum)
            if cached:
                self._entries.move_to_end(snapshot.content_checksum)
                return cached
            approved = {node.id for node in snapshot.nodes if node.kind == "METRIC"
                        and node.facts.get("authoritative") is True}
            rules = {edge.target for edge in snapshot.edges if edge.kind == "FILTERED_BY"
                     and edge.source in approved}
            nodes = {node.id: node for node in snapshot.nodes
                     if (node.kind != "METRIC" or node.id in approved)
                     and (node.kind != "BUSINESS_RULE" or node.id in rules)}
            edges = {edge.id: edge for edge in snapshot.edges
                     if edge.source in nodes and edge.target in nodes}
            index = ExplorerIndex(snapshot, network.subgraph(nodes), nodes, edges)
            self._entries[snapshot.content_checksum] = index
            if len(self._entries) > 16:
                self._entries.popitem(last=False)
            return index
