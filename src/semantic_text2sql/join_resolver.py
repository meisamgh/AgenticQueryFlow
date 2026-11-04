"""Shared bounded join traversal. Paths are evidence, never semantic correctness proof."""

from __future__ import annotations

from collections import deque
from itertools import combinations
from time import monotonic
from typing import Any, Literal

import networkx as nx
from pydantic import Field

from semantic_text2sql.models import SchemaInfo, StrictModel
from semantic_text2sql.semantic_catalog import schema_fingerprint
from semantic_text2sql.semantic_graph import GraphEdge, GraphSnapshot, digest


class JoinResolution(StrictModel):
    status: Literal["CONNECTED", "AMBIGUOUS", "DISCONNECTED", "BUDGET_EXHAUSTED"]
    tables: list[str]
    bridges: list[str] = Field(default_factory=list)
    relationships: list[GraphEdge] = Field(default_factory=list)
    explanation: str


def _join_identity(edge: GraphEdge) -> str:
    # Equality predicates are symmetric, including reversed composite declarations.
    return digest(
        sorted(
            sorted([(edge.source, child), (edge.target, parent)])
            for child, parent in zip(
                edge.facts["child_columns"], edge.facts["parent_columns"], strict=True
            )
        )
    )


def tables_connected(tables: set[str], pairs: list[tuple[str, str]]) -> bool:
    if len(tables) < 2:
        return True
    graph: nx.Graph[str, dict[str, Any], dict[str, Any]] = nx.Graph()
    graph.add_nodes_from(tables)
    graph.add_edges_from((a, b) for a, b in pairs if a in tables and b in tables)
    return bool(nx.is_connected(graph))


def declared_edges(schema: SchemaInfo) -> list[GraphEdge]:
    groups: dict[tuple[str, str, str], list[tuple[int, str, str]]] = {}
    for fk in schema.relationships:
        key = (fk.from_table, fk.to_table, fk.constraint_id or f"{fk.from_column}:{fk.to_column}")
        groups.setdefault(key, []).append((fk.ordinal, fk.from_column, fk.to_column))
    return [
        GraphEdge(
            id=digest([child, parent, key]),
            source=f"table:{child}",
            target=f"table:{parent}",
            kind="REFERENCES",
            provenance="declared_foreign_key",
            authoritative=True,
            facts={
                "child_columns": [row[1] for row in sorted(rows)],
                "parent_columns": [row[2] for row in sorted(rows)],
                "cardinality": "unknown",
                "fanout_risk": True,
            },
        )
        for (child, parent, key), rows in sorted(groups.items())
    ]


def resolve_joins(
    selected: list[str],
    schema: SchemaInfo,
    *,
    snapshot: GraphSnapshot | None = None,
    allowed_tables: set[str] | None = None,
    max_depth: int = 6,
    max_candidates: int = 64,
    timeout_seconds: float = 0.05,
    reject_ambiguous: bool = True,
) -> JoinResolution:
    deadline = monotonic() + max(0, timeout_seconds)
    bridge_order: list[str] = []
    allowed = {table.name for table in schema.tables}
    if allowed_tables is not None:
        allowed &= allowed_tables
    seeds = list(dict.fromkeys(selected))

    def result(
        status: Literal["CONNECTED", "AMBIGUOUS", "DISCONNECTED", "BUDGET_EXHAUSTED"],
        evidence: list[GraphEdge],
        message: str,
    ) -> JoinResolution:
        names = set(seeds)
        for edge in evidence:
            names.update([edge.source.removeprefix("table:"), edge.target.removeprefix("table:")])
        return JoinResolution(
            status=status,
            tables=[*seeds, *[name for name in bridge_order if name in names - set(seeds)]],
            bridges=[name for name in bridge_order if name in names - set(seeds)],
            relationships=evidence,
            explanation=message,
        )

    if not set(seeds) <= allowed:
        return result("DISCONNECTED", [], "A selected table is outside the permitted schema.")
    if snapshot is not None and (
        snapshot.db_id != schema.db_id or snapshot.schema_fingerprint != schema_fingerprint(schema)
    ):
        return result("DISCONNECTED", [], "Snapshot belongs to a different datasource.")
    if len(seeds) < 2:
        return result("CONNECTED", [], "No join required.")
    edges = snapshot.edges if snapshot else declared_edges(schema)
    graph: nx.MultiGraph[str, dict[str, Any], dict[str, Any]] = nx.MultiGraph()
    graph.add_nodes_from(allowed)
    forbidden = {
        _join_identity(edge)
        for edge in edges
        if edge.kind == "REFERENCES"
        and edge.facts.get("forbidden")
        and edge.facts.get("status") == "approved"
        and edge.facts.get("catalog_current")
    }
    fields = {table.name: {col.name for col in table.columns} for table in schema.tables}
    for edge in edges:
        if monotonic() >= deadline:
            return result("BUDGET_EXHAUSTED", [], "Join traversal deadline reached.")
        if edge.kind != "REFERENCES" or not edge.authoritative:
            continue
        a, b = edge.source.removeprefix("table:"), edge.target.removeprefix("table:")
        identity = _join_identity(edge)
        if (
            a in allowed
            and b in allowed
            and identity not in forbidden
            and set(edge.facts.get("child_columns", [])) <= fields[a]
            and set(edge.facts.get("parent_columns", [])) <= fields[b]
        ):
            graph.add_edge(a, b, key=edge.id, evidence=edge)
    evidence: dict[str, GraphEdge] = {}
    processed = 0
    for start, target in combinations(seeds, 2):
        queue: deque[tuple[str, list[str], list[GraphEdge]]] = deque([(start, [start], [])])
        legacy_visited = {start}
        candidates: list[tuple[tuple[int, int, int, int], list[GraphEdge]]] = []
        while queue:
            if monotonic() >= deadline or processed >= max_candidates:
                return result(
                    "BUDGET_EXHAUSTED", [], "Join traversal limit reached; no path accepted."
                )
            current, path, path_edges = queue.popleft()
            processed += 1
            if current == target:
                rank = (
                    sum(item.provenance != "approved_catalog" for item in path_edges),
                    sum(
                        item.facts.get("cardinality", "unknown") in {"unknown", "many_to_many"}
                        for item in path_edges
                    ),
                    sum(
                        bool(
                            item.facts.get(
                                "fanout_risk", item.facts.get("cardinality") != "one_to_one"
                            )
                        )
                        for item in path_edges
                    ),
                    len(path_edges),
                )
                candidates.append((rank, path_edges))
                if not reject_ambiguous:
                    break
                continue
            if len(path_edges) >= max_depth:
                continue
            for neighbor in sorted(graph.neighbors(current)):
                if neighbor in path or (not reject_ambiguous and neighbor in legacy_visited):
                    continue
                legacy_visited.add(neighbor)
                # Duplicate declared/catalog mappings are one candidate; approved metadata wins.
                variants: dict[str, GraphEdge] = {}
                for data in graph[current][neighbor].values():
                    edge = data["evidence"]
                    key = _join_identity(edge)
                    old = variants.get(key)
                    if old is None or edge.provenance == "approved_catalog":
                        variants[key] = edge
                choices = list(variants.values())
                for edge in choices if reject_ambiguous else choices[:1]:
                    queue.append((neighbor, [*path, neighbor], [*path_edges, edge]))
        if not candidates:
            return result("DISCONNECTED", [], "No authorized authoritative connecting path.")
        candidates.sort(key=lambda item: item[0])
        best = [path for rank, path in candidates if rank == candidates[0][0]]
        if len(best) > 1 and reject_ambiguous:
            return result("AMBIGUOUS", [], "Equally trusted join paths need a business choice.")
        current = start
        for edge in best[0]:
            child = edge.source.removeprefix("table:")
            parent = edge.target.removeprefix("table:")
            current = parent if current == child else child
            if current not in seeds and current not in bridge_order:
                bridge_order.append(current)
        evidence.update({edge.id: edge for edge in best[0]})
    return result(
        "CONNECTED",
        list(evidence.values()),
        "Authoritative paths found; cardinality/fanout still require SQL validation.",
    )
