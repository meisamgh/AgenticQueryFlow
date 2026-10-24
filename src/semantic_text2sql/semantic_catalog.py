"""Governed, versioned semantic data catalog: typed sidecar records and composition.

Blueprint (collaborative-agent v2, Phase A): compose live schema, verified offline
profiles and human-approved override records into one authoritative contract.
LLM output and heuristic confidence can never promote a fact to ``approved``
and machine-authored facts land as ``inferred`` until a human reviews them;
every approved fact must carry a ``human_review`` approval record. Workspace facts
observed by deterministic probes stay ``observed`` unless a data owner approves them.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from semantic_text2sql.models import DatabaseProfile, SchemaInfo, SemanticFilter, StrictModel

MetadataStatus = Literal[
    "observed", "inferred", "reviewed", "approved", "stale", "conflicting"
]
ColumnRole = Literal["identifier", "dimension", "time", "measure"]
EntityRole = Literal["declared_primary_key", "natural_key", "asserted"]
UpdateMode = Literal["append", "upsert", "historical_snapshot", "unknown"]
TimeGrain = Literal["daily", "weekly", "monthly", "trading_day"]
AggregationPolicy = Literal["additive", "semi_additive", "non_additive"]
JoinSource = Literal["declared_foreign_key", "observed_probe", "manual"]
Cardinality = Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many", "unknown"]

_STABLE_STATUSES = frozenset({"approved", "reviewed", "observed"})


class ApprovalRecord(StrictModel):
    """Human approval of a catalog fact. Machine output can never mint one."""

    approved_by: str = Field(min_length=1)
    approved_at: datetime
    approval_source: Literal["human_review"]
    evidence: list[str] = Field(default_factory=list)
    note: str | None = None


class ApprovableFact(StrictModel):
    status: MetadataStatus = "inferred"
    approval: ApprovalRecord | None = None

    @model_validator(mode="after")
    def _approval_matches_status(self) -> ApprovableFact:
        if self.approval is not None and self.status != "approved":
            raise ValueError("approval records require status 'approved'")
        if self.status == "approved" and self.approval is None:
            raise ValueError("approved facts require a human approval record")
        return self


class ColumnCatalogEntry(StrictModel):
    role: ColumnRole
    description: str | None = None
    semantic_type: str | None = None
    unit: str | None = None
    nullable: bool | None = None
    declared_type: str | None = None


class GrainFactEntry(ApprovableFact):
    keys: list[str] = Field(min_length=1)
    meaning: str = Field(min_length=1)
    entity_role: EntityRole


class TimeSemanticsEntry(StrictModel):
    event_time: str = Field(min_length=1)
    timezone: str | None = None
    update_mode: UpdateMode = "unknown"
    expected_frequency: str | None = None


class QualityContractEntry(StrictModel):
    required: list[str] = Field(default_factory=list)
    unique: list[list[str]] = Field(default_factory=list)
    positive: list[str] = Field(default_factory=list)

    def declared_fields(self) -> set[str]:
        """Every field the contract asserts something deterministic about."""
        return set(self.required) | {key for group in self.unique for key in group} | set(
            self.positive
        )


class TableCatalogEntry(ApprovableFact):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    relation: str = Field(min_length=1)
    description: str = Field(min_length=1)
    columns: dict[str, ColumnCatalogEntry] = Field(min_length=1)
    grain: GrainFactEntry | None = None
    time: TimeSemanticsEntry | None = None
    quality: QualityContractEntry = Field(default_factory=QualityContractEntry)


class JoinCatalogEntry(ApprovableFact):
    child_table: str = Field(min_length=1)
    child_columns: list[str] = Field(min_length=1)
    parent_table: str = Field(min_length=1)
    parent_columns: list[str] = Field(min_length=1)
    source: JoinSource
    cardinality: Cardinality = "unknown"
    forbidden: bool = False
    effective_time_column: str | None = None

    @model_validator(mode="after")
    def _key_shapes_match(self) -> JoinCatalogEntry:
        if len(self.child_columns) != len(self.parent_columns):
            raise ValueError("join sides must declare the same number of key columns")
        return self


class MetricCatalogEntry(ApprovableFact):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    label: str = Field(min_length=1)
    description: str = Field(min_length=1)
    source_relation: str = Field(min_length=1)
    formula: str = Field(min_length=1)
    required_fields: list[str] = Field(min_length=1)
    mandatory_filters: list[SemanticFilter] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    valid_time_grains: list[TimeGrain] = Field(min_length=1)
    unit: str = Field(min_length=1)
    format: str = Field(min_length=1)
    aggregation: AggregationPolicy = "non_additive"
    rollup: str | None = None
    missing_data_policy: str | None = None
    version: int = Field(default=1, ge=1)


class CatalogDocument(StrictModel):
    catalog_version: int = Field(ge=1)
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    source_db_id: str = Field(min_length=1)
    schema_fingerprint: str | None = None
    owner: str | None = None
    metadata_status: MetadataStatus = "inferred"
    entities: list[TableCatalogEntry] = Field(min_length=1)
    relationships: list[JoinCatalogEntry] = Field(default_factory=list)
    metrics: list[MetricCatalogEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_identifiers(self) -> CatalogDocument:
        entity_ids = [entity.id for entity in self.entities]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError("duplicate entity id in catalog document")
        metric_ids = [metric.id for metric in self.metrics]
        if len(metric_ids) != len(set(metric_ids)):
            raise ValueError("duplicate metric id in catalog document")
        declared_relations = {entity.relation for entity in self.entities}
        for join in self.relationships:
            undeclared = {join.child_table, join.parent_table} - declared_relations
            if undeclared:
                raise ValueError("join references relation missing from declared entities")
        for metric in self.metrics:
            entity = next(
                (item for item in self.entities if item.relation == metric.source_relation),
                None,
            )
            if entity is None:
                raise ValueError(f"metric {metric.id} references undeclared relation")
            unknown_fields = set(metric.required_fields) - entity.quality.declared_fields()
            if unknown_fields:
                raise ValueError(
                    f"metric {metric.id} requires fields the quality contract does not "
                    f"assert: {sorted(unknown_fields)}"
                )
        return self


class SemanticCatalogStore:
    """SQLite-free JSON sidecar store with mtime-cache, mirroring GlossaryStore."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root.resolve() if root else None
        self._cache: dict[str, tuple[int, CatalogDocument]] = {}

    def load(self, db_id: str) -> CatalogDocument | None:
        if self.root is None:
            return None
        path = self.root / f"{db_id}.json"
        if not path.is_file():
            return None
        modified_ns = path.stat().st_mtime_ns
        cached = self._cache.get(db_id)
        if cached is not None and cached[0] == modified_ns:
            return cached[1]
        document = CatalogDocument.model_validate_json(path.read_text(encoding="utf-8"))
        self._cache[db_id] = (modified_ns, document)
        return document


def schema_fingerprint(schema: SchemaInfo) -> str:
    """Deterministic fingerprint over live tables/columns; changes on any drift."""
    payload = {
        "dialect": schema.dialect,
        "tables": sorted(
            (
                table.name,
                sorted(
                    (column.name, column.data_type, column.primary_key)
                    for column in table.columns
                ),
            )
            for table in schema.tables
        ),
        "relationships": sorted(
            (
                item.from_table,
                item.from_column,
                item.to_table,
                item.to_column,
            )
            for item in schema.relationships
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def compose_entry_status(
    entry: ApprovableFact,
    *,
    relation_exists: bool,
    missing_columns: set[str] | None = None,
) -> MetadataStatus:
    """Apply the precedence rules for live drift; never promotes a fact.

    Missing support downgrades stable statuses (approved/reviewed/observed) to
    ``stale``. ``inferred``/``observed``-candidates stay as declared regardless of
    live state, because they never claimed to be authoritative.
    """
    drift = (not relation_exists) or bool(missing_columns)
    if drift and entry.status in _STABLE_STATUSES:
        return "stale"
    return entry.status


def compose_document(
    document: CatalogDocument,
    schema: SchemaInfo,
    profile: DatabaseProfile | None,
) -> dict[str, object]:
    """Compose the catalog with live schema into one read-only response payload."""
    live_tables = {table.name: table for table in schema.tables}
    live_fingerprint = schema_fingerprint(schema)
    if document.schema_fingerprint and document.schema_fingerprint != live_fingerprint:
        fingerprint_status: MetadataStatus = "stale"
    elif document.schema_fingerprint:
        fingerprint_status = "approved"
    else:
        fingerprint_status = "observed"
    entities: list[dict[str, object]] = []
    for entity in document.entities:
        table = live_tables.get(entity.relation)
        missing = set(entity.columns) - {column.name for column in table.columns} if table else set(
            entity.columns
        )
        entities.append(
            {
                **entity.model_dump(exclude={"status", "approval"}),
                "declared_status": entity.status,
                "live_status": compose_entry_status(
                    entity, relation_exists=table is not None, missing_columns=missing
                ),
                "catalog_version": document.catalog_version,
            }
        )
    metrics: list[dict[str, object]] = []
    for metric in document.metrics:
        metrics.append(
            {
                **metric.model_dump(exclude={"status", "approval"}),
                "declared_status": metric.status,
                "live_status": compose_entry_status(
                    metric, relation_exists=metric.source_relation in live_tables
                ),
                "catalog_version": document.catalog_version,
            }
        )
    relationships: list[dict[str, object]] = []
    for join in document.relationships:
        relationships.append(
            {
                **join.model_dump(exclude={"status", "approval"}),
                "declared_status": join.status,
                "live_status": compose_entry_status(
                    join,
                    relation_exists=(
                        join.child_table in live_tables and join.parent_table in live_tables
                    ),
                ),
            }
        )
    profile_tables = (
        {item.table: item for item in profile.tables} if profile else {}
    )
    return {
        "db_id": document.source_db_id,
        "catalog_version": document.catalog_version,
        "metadata_status": document.metadata_status,
        "owner": document.owner,
        "declared_schema_fingerprint": document.schema_fingerprint,
        "live_schema_fingerprint": live_fingerprint,
        "fingerprint_status": fingerprint_status,
        "entity_count": len(entities),
        "entities": entities,
        "relationships": relationships,
        "metrics": metrics,
        "profiled_tables": sorted(profile_tables),
    }
