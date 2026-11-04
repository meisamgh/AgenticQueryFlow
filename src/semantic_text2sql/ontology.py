"""Read-only local ontology adapter. External providers are deliberately not enabled."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Protocol

from semantic_text2sql.glossary import GlossaryStore
from semantic_text2sql.graph_grounding import resolve_metrics
from semantic_text2sql.models import DatabaseProfile, SchemaInfo, SemanticContract
from semantic_text2sql.semantic_catalog import SemanticCatalogStore, schema_fingerprint
from semantic_text2sql.semantic_graph import SemanticGraphStore, graph_identity


class OntologyAdapter(Protocol):
    def lookup(self, query: str, *, allowed_tables: set[str]) -> dict[str, Any]: ...


class LocalOntologyAdapter:
    def __init__(
        self,
        schema: SchemaInfo,
        profile: DatabaseProfile | None,
        catalogs: SemanticCatalogStore,
        graphs: SemanticGraphStore,
        glossaries: GlossaryStore,
    ) -> None:
        self.schema, self.profile = schema, profile
        self.catalogs, self.graphs, self.glossaries = catalogs, graphs, glossaries

    def lookup(self, query: str, *, allowed_tables: set[str]) -> dict[str, Any]:
        catalog = self.catalogs.load(self.schema.db_id)
        snapshot = self.graphs.load(
            self.schema.db_id,
            identity=graph_identity(
                self.schema, catalog, self.profile, self.glossaries.load(self.schema.db_id)
            ),
        )
        if snapshot is None:
            return {"available": False, "reason": "No compatible offline snapshot"}
        _, metrics = resolve_metrics(query, self.schema, catalog, SemanticContract())
        metrics = [
            metric
            for metric in metrics
            if metric["source_relation"] in allowed_tables
            and all(
                (field.rsplit(".", 1)[0] if "." in field else metric["source_relation"])
                in allowed_tables
                for field in metric["dimensions"]
            )
            and all(
                predicate["operand"].rsplit(".", 1)[0] in allowed_tables
                for predicate in metric["mandatory_filters"]
            )
            and all(
                join["child_table"] in allowed_tables and join["parent_table"] in allowed_tables
                for join in metric["required_joins"]
            )
        ]
        terms = set(query.casefold().split())
        relationships = [
            edge.model_dump(mode="json")
            for edge in snapshot.edges
            if edge.kind == "REFERENCES"
            and edge.authoritative
            and edge.source.removeprefix("table:") in allowed_tables
            and edge.target.removeprefix("table:") in allowed_tables
            and (
                "join" in terms
                or "relationship" in terms
                or edge.source.removeprefix("table:").casefold() in terms
                or edge.target.removeprefix("table:").casefold() in terms
            )
        ]
        concepts = []
        for node in snapshot.nodes:
            if node.kind != "BUSINESS_CONCEPT" or not terms & set(
                node.facts["term"].casefold().split()
            ):
                continue
            targets = [edge.target for edge in snapshot.edges if edge.source == node.id]
            if targets and all(
                target.removeprefix("column:").rsplit(".", 1)[0] in allowed_tables
                for target in targets
            ):
                concepts.append({"id": node.id, **node.facts, "authoritative": False})
        return {
            "available": True,
            "evidence_type": "APPROVED_ONTOLOGY",
            "db_id": self.schema.db_id,
            "schema_fingerprint": schema_fingerprint(self.schema),
            "catalog_version": catalog.catalog_version if catalog else None,
            "authorized_tables": sorted(allowed_tables),
            "snapshot_id": snapshot.identity,
            "metrics": metrics[:4],
            "relationship_definitions": relationships[:8],
            "concept_suggestions": concepts[:4],
        }


def configured_local_ontology(
    schema: SchemaInfo, profile: DatabaseProfile | None
) -> OntologyAdapter:
    return LocalOntologyAdapter(
        schema,
        profile,
        SemanticCatalogStore(
            Path(os.environ.get("TEXT2SQL_SEMANTIC_CATALOG_ROOT", "data/semantic_catalogs"))
        ),
        SemanticGraphStore(
            Path(os.environ.get("TEXT2SQL_GRAPH_ROOT", "graphs")),
            persistence_enabled=os.environ.get(
                "TEXT2SQL_GRAPH_PERSISTENCE_ENABLED", "false").casefold() == "true",
        ),
        GlossaryStore(Path(os.environ.get("TEXT2SQL_GLOSSARY_ROOT", "data/business_glossaries"))),
    )
