"""Offline, provenance-preserving semantic graph; never contains business rows."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Literal

import networkx as nx
from pydantic import Field, model_validator

from semantic_text2sql.glossary import BusinessGlossary
from semantic_text2sql.graph_grounding import resolve_metrics
from semantic_text2sql.models import DatabaseProfile, SchemaInfo, SemanticContract, StrictModel
from semantic_text2sql.semantic_catalog import CatalogDocument, schema_fingerprint

BUILDER_VERSION = "1"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class GraphNode(StrictModel):
    id: str
    kind: Literal["TABLE", "COLUMN", "METRIC", "DIMENSION", "BUSINESS_CONCEPT", "BUSINESS_RULE"]
    facts: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(StrictModel):
    id: str
    source: str
    target: str
    kind: Literal[
        "HAS_COLUMN",
        "REFERENCES",
        "DEPENDS_ON",
        "USES_COLUMN",
        "FILTERED_BY",
        "TIME_BY",
        "GROUPABLE_BY",
    ]
    provenance: str
    authoritative: bool = False
    facts: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_join_mapping(self) -> GraphEdge:
        if self.kind == "REFERENCES":
            child, parent = self.facts.get("child_columns"), self.facts.get("parent_columns")
            if (
                not isinstance(child, list)
                or not isinstance(parent, list)
                or not child
                or len(child) != len(parent)
                or not all(isinstance(item, str) and item for item in [*child, *parent])
            ):
                raise ValueError("REFERENCES needs an exact nonempty composite mapping")
        return self


class GraphSnapshot(StrictModel):
    db_id: str
    builder_version: Literal["1"] = "1"
    identity: str
    schema_fingerprint: str
    catalog_version: int | None = None
    profile_version: str | None = None
    built_at: datetime
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    content_checksum: str

    @model_validator(mode="after")
    def validate_references(self) -> GraphSnapshot:
        if self.content_checksum != digest(
            self.model_dump(mode="json", exclude={"content_checksum", "built_at"})
        ):
            raise ValueError("Graph snapshot checksum mismatch")
        names = {node.id for node in self.nodes}
        if len(names) != len(self.nodes) or len({edge.id for edge in self.edges}) != len(
            self.edges
        ):
            raise ValueError("Duplicate graph identifier")
        if any(edge.source not in names or edge.target not in names for edge in self.edges):
            raise ValueError("Dangling graph edge")
        return self

    def network(self) -> nx.MultiDiGraph[str, dict[str, Any], dict[str, Any]]:
        graph: nx.MultiDiGraph[str, dict[str, Any], dict[str, Any]] = nx.MultiDiGraph()
        graph.graph.update(db_id=self.db_id, identity=self.identity,
                           content_checksum=self.content_checksum)
        for node in self.nodes:
            graph.add_node(node.id, kind=node.kind, **node.facts)
        for edge in self.edges:
            graph.add_edge(edge.source, edge.target, key=edge.id, evidence=edge)
        nx.freeze(graph)
        return graph


def graph_identity(
    schema: SchemaInfo,
    catalog: CatalogDocument | None = None,
    profile: DatabaseProfile | None = None,
    glossary: BusinessGlossary | None = None,
) -> str:
    # Hash metadata including FK constraint/ordinal, never persist sampled profile values.
    return digest(
        [
            BUILDER_VERSION,
            schema.model_dump(),
            catalog.model_dump(mode="json") if catalog else None,
            profile.model_dump() if profile else None,
            glossary.model_dump() if glossary else None,
        ]
    )


def compile_graph(
    schema: SchemaInfo,
    catalog: CatalogDocument | None = None,
    profile: DatabaseProfile | None = None,
    glossary: BusinessGlossary | None = None,
) -> GraphSnapshot:
    """Explicit offline compilation from existing metadata; no I/O, inference or probes."""
    if any(item is not None and item.db_id != schema.db_id for item in (profile, glossary)):
        raise ValueError("Datasource mismatch")
    if catalog and catalog.source_db_id != schema.db_id:
        raise ValueError("Catalog datasource mismatch")
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    fields = {table.name: {column.name for column in table.columns} for table in schema.tables}
    table_profiles = {item.table: item for item in profile.tables} if profile else {}

    def node(name: str, kind: Any, **facts: Any) -> str:
        nodes[name] = GraphNode(id=name, kind=kind, facts=facts)
        return name

    def edge(
        source: str,
        target: str,
        kind: Any,
        provenance: str,
        authoritative: bool = False,
        **facts: Any,
    ) -> None:
        if source in nodes and target in nodes:
            identity = digest([source, target, kind, provenance, facts])
            edges.append(
                GraphEdge(
                    id=identity,
                    source=source,
                    target=target,
                    kind=kind,
                    provenance=provenance,
                    authoritative=authoritative,
                    facts=facts,
                )
            )

    for table in schema.tables:
        observed = table_profiles.get(table.name)
        tid = node(
            f"table:{table.name}",
            "TABLE",
            relation=table.name,
            primary_key=[col.name for col in table.columns if col.primary_key],
            grain=(
                observed.grain
                if observed
                else "one row per "
                + " + ".join(col.name for col in table.columns if col.primary_key)
                if any(col.primary_key for col in table.columns)
                else None
            ),
        )
        for col in table.columns:
            cid = node(
                f"column:{table.name}.{col.name}",
                "COLUMN",
                table=table.name,
                column=col.name,
                type=col.data_type,
                primary_key=col.primary_key,
            )
            edge(tid, cid, "HAS_COLUMN", "live_schema", True)

    groups: dict[tuple[str, str, str], list[Any]] = {}
    for fk in schema.relationships:
        # Unnamed FKs remain independent; do not invent composite mappings.
        key = (fk.from_table, fk.to_table, fk.constraint_id or f"{fk.from_column}:{fk.to_column}")
        groups.setdefault(key, []).append(fk)
    for (child, parent, constraint), mappings in sorted(groups.items()):
        mappings.sort(key=lambda item: item.ordinal)
        edge(
            f"table:{child}",
            f"table:{parent}",
            "REFERENCES",
            "declared_foreign_key",
            True,
            child_columns=[item.from_column for item in mappings],
            parent_columns=[item.to_column for item in mappings],
            constraint_id=constraint,
            cardinality="unknown",
            fanout_risk=True,
        )
    current_catalog = bool(
        catalog
        and catalog.metadata_status not in {"stale", "conflicting"}
        and (
            not catalog.schema_fingerprint
            or catalog.schema_fingerprint == schema_fingerprint(schema)
        )
    )
    if catalog:
        valid_metric_ids: set[str] = set()
        for join in catalog.relationships:
            valid = set(join.child_columns) <= fields.get(join.child_table, set()) and set(
                join.parent_columns
            ) <= fields.get(join.parent_table, set())
            if not valid:
                continue
            edge(
                f"table:{join.child_table}",
                f"table:{join.parent_table}",
                "REFERENCES",
                "approved_catalog" if join.status == "approved" else "catalog_candidate",
                current_catalog and join.status == "approved" and not join.forbidden,
                join_source=join.source,
                catalog_current=current_catalog,
                **join.model_dump(mode="json", exclude={"source"}),
            )
        for metric in catalog.metrics:
            try:
                _, resolved = resolve_metrics(metric.id, schema, catalog, SemanticContract())
                valid = any(item["id"] == metric.id for item in resolved)
            except ValueError:
                valid = False
            if valid:
                valid_metric_ids.add(metric.id)
            mid = node(
                f"metric:{metric.id}",
                "METRIC",
                **metric.model_dump(mode="json"),
                authoritative=current_catalog and metric.status == "approved" and valid,
                catalog_version=catalog.catalog_version,
            )
            for column in metric.required_fields:
                edge(
                    mid,
                    f"column:{metric.source_relation}.{column}",
                    "USES_COLUMN",
                    "semantic_catalog",
                    current_catalog and metric.status == "approved" and valid,
                )
            for column in metric.dimensions:
                qualified = column if "." in column else f"{metric.source_relation}.{column}"
                edge(mid, f"column:{qualified}", "GROUPABLE_BY", "semantic_catalog")
            for index, predicate in enumerate(metric.mandatory_filters):
                rid = node(
                    f"rule:{metric.id}:{index}",
                    "BUSINESS_RULE",
                    predicate=predicate.model_dump(mode="json"),
                )
                edge(mid, rid, "FILTERED_BY", "semantic_catalog")
        for entity in catalog.entities:
            for column, definition in entity.columns.items():
                cid = f"column:{entity.relation}.{column}"
                if cid not in nodes:
                    continue
                if definition.role == "dimension":
                    did = node(
                        f"dimension:{entity.relation}.{column}",
                        "DIMENSION",
                        table=entity.relation,
                        column=column,
                    )
                    edge(did, cid, "USES_COLUMN", "semantic_catalog")
                if definition.role == "time":
                    edge(f"table:{entity.relation}", cid, "TIME_BY", "semantic_catalog")
        for metric in catalog.metrics:
            for dependency in metric.dependencies:
                edge(
                    f"metric:{metric.id}",
                    f"metric:{dependency}",
                    "DEPENDS_ON",
                    "semantic_catalog",
                    current_catalog
                    and metric.id in valid_metric_ids
                    and dependency in valid_metric_ids,
                )
    if profile:
        for rel in profile.relationships:
            edge(
                f"table:{rel.child_table}",
                f"table:{rel.parent_table}",
                "REFERENCES",
                "inferred_profile" if rel.inferred else "observed_profile",
                False,
                child_columns=rel.child_columns or [rel.child_column],
                parent_columns=rel.parent_columns or [rel.parent_column],
                cardinality=rel.type.lower(),
                fanout_risk=not rel.child_key_unique,
            )
    if glossary:
        for term in glossary.terms:
            cid = node(
                f"concept:{term.term}",
                "BUSINESS_CONCEPT",
                term=term.term,
                definition=term.definition,
                source=glossary.source,
                version=glossary.version,
                formula=term.formula,
                grain=term.grain,
            )
            for column in term.columns:
                edge(cid, f"column:{column}", "USES_COLUMN", "business_glossary")
    payload = {
        "db_id": schema.db_id,
        "builder_version": BUILDER_VERSION,
        "identity": graph_identity(schema, catalog, profile, glossary),
        "schema_fingerprint": schema_fingerprint(schema),
        "catalog_version": catalog.catalog_version if catalog else None,
        "profile_version": profile.profiled_at if profile else None,
        "nodes": [item.model_dump(mode="json") for item in nodes.values()],
        "edges": [
            item.model_dump(mode="json") for item in {item.id: item for item in edges}.values()
        ],
    }
    return GraphSnapshot.model_validate(
        {**payload, "built_at": datetime.now(UTC), "content_checksum": digest(payload)}
    )


class SemanticGraphStore:
    """Versioned JSON snapshots; atomic writes, bounded cache and fail-closed loads."""

    def __init__(self, root: Path, *, persistence_enabled: bool = True) -> None:
        self.root = root
        # Explicit standalone/CLI stores retain compatibility; application startup
        # passes its independently default-OFF persistence setting.
        self.persistence_enabled = persistence_enabled
        self._cache: dict[
            str,
            tuple[int, int, GraphSnapshot, nx.MultiDiGraph[str, dict[str, Any], dict[str, Any]]],
        ] = {}
        self._lock = RLock()

    def _path(self, db_id: str) -> Path:
        return self.root / f"{digest(db_id)}.json"

    def remember(self, snapshot: GraphSnapshot) -> None:
        """Cache verified metadata without any filesystem access unless opted in."""
        if self.persistence_enabled:
            self.save(snapshot)
            return
        snapshot = GraphSnapshot.model_validate(snapshot.model_dump())
        with self._lock:
            if len(self._cache) >= 32 and snapshot.db_id not in self._cache:
                self._cache.pop(next(iter(self._cache)))
            self._cache[snapshot.db_id] = (0, 0, snapshot, snapshot.network())

    def save(self, snapshot: GraphSnapshot) -> Path:
        snapshot = GraphSnapshot.model_validate(snapshot.model_dump())
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(snapshot.db_id)
        with self._lock:
            fd, name = tempfile.mkstemp(dir=self.root, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(snapshot.model_dump_json())
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(name, path)
                self._cache.pop(snapshot.db_id, None)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        return path

    def load(self, db_id: str, *, identity: str) -> GraphSnapshot | None:
        with self._lock:
            if not self.persistence_enabled:
                cached = self._cache.get(db_id)
                return (cached[2].model_copy(deep=True)
                        if cached and cached[2].identity == identity else None)
            try:
                path = self._path(db_id)
                stat = path.stat()
                if stat.st_size > 10_000_000:
                    return None
                cached = self._cache.get(db_id)
                if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
                    snapshot = cached[2]
                else:
                    snapshot = GraphSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
                    if len(self._cache) >= 32:
                        self._cache.pop(next(iter(self._cache)))
                    self._cache[db_id] = (
                        stat.st_mtime_ns,
                        stat.st_size,
                        snapshot,
                        snapshot.network(),
                    )
                if snapshot.db_id != db_id or snapshot.identity != identity:
                    return None
                return snapshot.model_copy(deep=True)
            except (OSError, ValueError, TypeError):
                self._cache.pop(db_id, None)
                return None

    def network(
        self, db_id: str, *, identity: str
    ) -> nx.MultiDiGraph[str, dict[str, Any], dict[str, Any]] | None:
        with self._lock:
            if self.persistence_enabled and self.load(db_id, identity=identity) is None:
                return None
            cached = self._cache.get(db_id)
            return cached[3] if cached and cached[2].identity == identity else None
