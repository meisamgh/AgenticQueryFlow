"""Deterministic catalog verification: evidence-backed probes, never auto-approval.

Read-only SQL probes run against the live database and their outcomes are
combined with the declared catalog statuses under the blueprint precedence rules:

- ``approved``/``reviewed``/``observed`` facts contradicted by evidence become
  ``conflicting``; missing live support downgrades them to ``stale`` instead.
- ``inferred`` facts are never promoted by a passing probe; probes
  can only disprove or leave declared facts untouched.
- A probe that cannot run (connection or SQL error) is recorded as ``skipped``
  evidence and never changes the declared status.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field
from sqlglot import parse_one

from semantic_text2sql.database import DatabaseRegistry
from semantic_text2sql.models import SchemaInfo, StrictModel
from semantic_text2sql.postgres import PostgresRegistry
from semantic_text2sql.semantic_catalog import (
    CatalogDocument,
    JoinCatalogEntry,
    MetadataStatus,
    MetricCatalogEntry,
    schema_fingerprint,
)

ProbeOutcome = Literal["pass", "fail", "skipped"]
ProbeKind = Literal[
    "relation_exists",
    "columns_match",
    "grain_unique",
    "required_not_null",
    "positive_measure",
    "date_coverage",
]
ProbeRunner = Callable[[str], list[dict[str, Any]]]
_STABLE = frozenset({"approved", "reviewed", "observed"})


class ProbeEvidence(StrictModel):
    evidence_id: str = Field(pattern=r"^probe-[a-f0-9]{12}$")
    probe: ProbeKind
    status: ProbeOutcome
    detail: str
    sql: str | None = None
    observed_at: datetime
    exact: bool = True


class ClaimVerification(StrictModel):
    subject: str
    subject_kind: Literal["entity", "grain", "metric", "relationship", "fingerprint"]
    status: MetadataStatus
    evidence_ids: list[str] = Field(default_factory=list)


class VerificationReport(StrictModel):
    db_id: str
    catalog_version: int
    live_schema_fingerprint: str
    fingerprint_status: MetadataStatus
    generated_at: datetime
    claims: list[ClaimVerification]
    summary: dict[str, int]

    @property
    def conflicts(self) -> list[ClaimVerification]:
        return [claim for claim in self.claims if claim.status == "conflicting"]


def _uuid() -> str:
    return f"probe-{uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(UTC)


def _qualified(relation: str, dialect: str) -> str:
    """Quote a possibly schema-qualified relation; plain for sqlite."""
    parts = [part for part in relation.split(".") if part] or [relation]
    if dialect == "sqlite":
        return ".".join(parts)
    return ".".join('"' + part.replace('"', '""') + '"' for part in parts)


def _is_missing_relation(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".casefold()
    tokens = ("no such table", "undefinedtable", "does not exist", "database_not_found")
    return any(token in text for token in tokens)


def sqlite_runner(registry: DatabaseRegistry, db_id: str, *, row_limit: int = 200) -> ProbeRunner:
    def run(sql: str) -> list[dict[str, Any]]:
        connection = registry.connect(db_id)
        try:
            cursor = connection.execute(sql)
            names = [item[0] for item in cursor.description or ()]
            return [dict(zip(names, row, strict=False)) for row in cursor.fetchmany(row_limit)]
        finally:
            connection.close()

    return run


def postgres_runner(registry: PostgresRegistry, db_id: str, *, row_limit: int = 200) -> ProbeRunner:
    def run(sql: str) -> list[dict[str, Any]]:
        dialect = registry.dialect_for(db_id)
        if dialect != "postgres":
            native = parse_one(sql, read="postgres").sql(dialect=dialect)
            names, rows, _ = registry.execute(db_id, native, max_rows=row_limit)
            return [dict(zip(names, row, strict=False)) for row in rows]
        with registry.connect(db_id) as connection, connection.cursor() as cursor:
            cursor.execute(sql)
            names = [item.name for item in cursor.description or ()]
            return [dict(zip(names, row, strict=False)) for row in cursor.fetchmany(row_limit)]

    return run


def _probe(
    runner: ProbeRunner,
    probe: ProbeKind,
    sql: str,
    *,
    passing: Callable[[list[dict[str, Any]]], bool],
    fail_detail: str,
    pass_detail: str,
) -> tuple[ProbeEvidence, bool]:
    """Run one probe SQL; return (evidence, holds).

    A missing relation is a semantic ``fail`` for the probe; transient
    connection/SQL errors are recorded as ``skipped`` and change nothing.
    """
    observations = {"sql": sql}
    try:
        rows = runner(sql)
    except Exception as exc:  # noqa: BLE001 - probe errors are evidence, not crashes
        if _is_missing_relation(exc):
            evidence = ProbeEvidence(
                evidence_id=_uuid(),
                probe=probe,
                status="fail",
                detail=f"relation unavailable: {type(exc).__name__}: {str(exc)[:120]}",
                sql=sql,
                observed_at=_now(),
            )
            return evidence, False
        evidence = ProbeEvidence(
            evidence_id=_uuid(),
            probe=probe,
            status="skipped",
            detail=f"{type(exc).__name__}: {str(exc)[:140]}",
            sql=None,
            observed_at=_now(),
        )
        return evidence, False
    holds = bool(passing(rows))
    evidence = ProbeEvidence(
        evidence_id=_uuid(),
        probe=probe,
        status="pass" if holds else "fail",
        detail=pass_detail if holds else fail_detail,
        sql=observations["sql"],
        observed_at=_now(),
    )
    return evidence, holds


def _capture_covered(rows: list[dict[str, Any]], sink: list[dict[str, Any]]) -> bool:
    """Record the coverage row, then report whether an observation exists."""
    sink.extend(rows[:1])
    return bool(rows) and rows[0].get("first_observed") is not None


def _coverage_passing(
    sink: list[dict[str, Any]],
) -> Callable[[list[dict[str, Any]]], bool]:
    """Bind the coverage sink once so the probe callback stays single-argument."""
    return lambda rows: _capture_covered(rows, sink)


def _combine_status(
    declared: MetadataStatus, *, relation_missing: bool, contradiction: bool
) -> MetadataStatus:
    if relation_missing:
        return "stale" if declared in _STABLE else declared
    if contradiction:
        return "conflicting" if declared in _STABLE else declared
    return declared


def _schema_column_names(schema: SchemaInfo, relation: str) -> set[str] | None:
    for table in schema.tables:
        if table.name == relation:
            return {column.name for column in table.columns}
    return None


def _static_evidence(probe: ProbeKind, status: ProbeOutcome, detail: str) -> ProbeEvidence:
    return ProbeEvidence(
        evidence_id=_uuid(),
        probe=probe,
        status=status,
        detail=detail,
        sql=None,
        observed_at=_now(),
    )


def _verify_metric(
    metric: MetricCatalogEntry,
    schema: SchemaInfo,
    dialect: str,
) -> tuple[MetadataStatus, list[ProbeEvidence]]:
    live_columns = _schema_column_names(schema, metric.source_relation)
    evidence: list[ProbeEvidence] = []
    if live_columns is None:
        evidence.append(
            _static_evidence(
                "columns_match",
                "fail",
                f"source relation {metric.source_relation} is not present in the live schema",
            )
        )
        return (
            _combine_status(metric.status, relation_missing=True, contradiction=False),
            evidence,
        )
    missing = set(metric.required_fields) - live_columns
    if missing:
        evidence.append(
            _static_evidence(
                "columns_match",
                "fail",
                f"metric fields absent from the live relation: {sorted(missing)}",
            )
        )
        return _combine_status(metric.status, relation_missing=False, contradiction=True), evidence
    evidence.append(
        _static_evidence(
            "columns_match", "pass", f"required fields present: {sorted(metric.required_fields)}"
        )
    )
    return _combine_status(metric.status, relation_missing=False, contradiction=False), evidence


def _verify_join(
    join: JoinCatalogEntry,
    schema: SchemaInfo,
) -> tuple[MetadataStatus, list[ProbeEvidence]]:
    evidence: list[ProbeEvidence] = []
    if join.source != "declared_foreign_key":
        evidence.append(
            _static_evidence(
                "columns_match",
                "pass",
                "non-declared join stays as its documented provenance",
            )
        )
        return _combine_status(join.status, relation_missing=False, contradiction=False), evidence
    declared_keys = {
        (item.from_table, item.from_column, item.to_table, item.to_column)
        for item in schema.relationships
    }
    matches = all(
        key in declared_keys
        for key in zip(
            [join.child_table] * len(join.child_columns),
            join.child_columns,
            [join.parent_table] * len(join.parent_columns),
            join.parent_columns,
            strict=True,
        )
    )
    if not matches:
        evidence.append(
            _static_evidence(
                "columns_match",
                "fail",
                "approved declared foreign keys must exist in the live constraints",
            )
        )
        return _combine_status(join.status, relation_missing=False, contradiction=True), evidence
    evidence.append(_static_evidence("columns_match", "pass", "declared foreign keys present"))
    return _combine_status(join.status, relation_missing=False, contradiction=False), evidence


def verify_catalog(
    document: CatalogDocument,
    schema: SchemaInfo,
    runner: ProbeRunner,
    dialect: str,
) -> VerificationReport:
    """Verify every declared fact and return one evidence-backed report."""
    claims: list[ClaimVerification] = []
    live_fingerprint = schema_fingerprint(schema)

    declared_fingerprint = document.schema_fingerprint
    if declared_fingerprint and declared_fingerprint != live_fingerprint:
        fingerprint_status: MetadataStatus = "stale"
    elif declared_fingerprint:
        fingerprint_status = "approved"
    else:
        fingerprint_status = "observed"

    for entity in document.entities:
        evidence: list[ProbeEvidence] = []
        relation_missing = False
        contradiction = False
        existence_ev, exists = _probe(
            runner,
            "relation_exists",
            f"SELECT COUNT(*) AS n FROM {_qualified(entity.relation, dialect)}",
            passing=lambda _rows: True,
            fail_detail=f"relation {entity.relation} could not be executed",
            pass_detail=f"relation {entity.relation} is queryable",
        )
        if existence_ev.status == "skipped":
            relation_missing = False  # cannot verify; leave declared status untouched
        elif not exists:
            relation_missing = True
        evidence.append(existence_ev)

        live_columns = _schema_column_names(schema, entity.relation)
        if live_columns is not None:
            missing_columns = set(entity.columns) - live_columns
            columns_ev = _static_evidence(
                "columns_match",
                "fail" if missing_columns else "pass",
                (
                    f"declared columns absent from the live relation: {sorted(missing_columns)}"
                    if missing_columns
                    else f"all {len(entity.columns)} declared columns are live"
                ),
            )
            evidence.append(columns_ev)
            contradiction = bool(missing_columns)

        if entry_grain := entity.grain:
            keys = ", ".join(entry_grain.keys)
            duplicate_ev, holds = _probe(
                runner,
                "grain_unique",
                (
                    f"SELECT {keys}, COUNT(*) AS duplicate_rows "
                    f"FROM {_qualified(entity.relation, dialect)} "
                    f"GROUP BY {keys} HAVING COUNT(*) > 1"
                ),
                passing=lambda rows: not rows,
                fail_detail=(
                    f"declared grain {keys} produced duplicate rows; grain is not one-row-per-key"
                ),
                pass_detail=f"grain {keys} is unique on the live relation",
            )
            duplicate_failed = duplicate_ev.status == "fail"
            grain_relation_missing = (
                duplicate_failed and "relation unavailable" in duplicate_ev.detail
            )
            grain_status = _combine_status(
                entry_grain.status,
                relation_missing=grain_relation_missing,
                contradiction=duplicate_ev.status == "fail" and not grain_relation_missing,
            )
            evidence.append(duplicate_ev)
            claims.append(
                ClaimVerification(
                    subject=f"{entity.id}.grain",
                    subject_kind="grain",
                    status=grain_status,
                    evidence_ids=[duplicate_ev.evidence_id],
                )
            )

        required = list(entity.quality.required)
        if required and existence_ev.status in ("pass", "skipped") and not relation_missing:
            where = " OR ".join(f"{column} IS NULL" for column in required)
            null_ev, _holds = _probe(
                runner,
                "required_not_null",
                (
                    f"SELECT COUNT(*) AS null_rows "
                    f"FROM {_qualified(entity.relation, dialect)} WHERE {where}"
                ),
                passing=lambda rows: int(rows[0].get("null_rows", 1)) == 0 if rows else False,
                fail_detail=f"required columns produced NULL rows: {required}",
                pass_detail=f"required columns are fully populated: {required}",
            )
            evidence.append(null_ev)
            if null_ev.status == "fail":
                contradiction = True

        for column in entity.quality.positive:
            positive_ev, _holds = _probe(
                runner,
                "positive_measure",
                (
                    f"SELECT COUNT(*) AS bad_rows FROM {_qualified(entity.relation, dialect)} "
                    f"WHERE {column} IS NULL OR {column} <= 0"
                ),
                passing=lambda rows: int(rows[0].get("bad_rows", 1)) == 0 if rows else False,
                fail_detail=f"{column} contains NULL or non-positive values",
                pass_detail=f"{column} is populated and positive",
            )
            evidence.append(positive_ev)
            if positive_ev.status == "fail":
                contradiction = True

        if entity.time is not None and not relation_missing:
            time_column = entity.time.event_time
            captured: list[dict[str, Any]] = []
            coverage_ev, _holds = _probe(
                runner,
                "date_coverage",
                (
                    f"SELECT MIN({time_column}) AS first_observed, "
                    f"MAX({time_column}) AS last_observed "
                    f"FROM {_qualified(entity.relation, dialect)}"
                ),
                passing=_coverage_passing(captured),
                fail_detail=f"time column {time_column} has no observed values",
                pass_detail="time coverage observed with exact bounds",
            )
            if coverage_ev.status == "pass" and captured:
                coverage_ev = coverage_ev.model_copy(
                    update={
                        "detail": (
                            f"time coverage of {time_column}: "
                            f"first={captured[0]['first_observed']!s} "
                            f"last={captured[0]['last_observed']!s}"
                        )
                    }
                )
            evidence.append(coverage_ev)

        entity_status = _combine_status(
            entity.status, relation_missing=relation_missing, contradiction=contradiction
        )
        claims.append(
            ClaimVerification(
                subject=f"{entity.relation}",
                subject_kind="entity",
                status=entity_status,
                evidence_ids=[item.evidence_id for item in evidence],
            )
        )

    for metric in document.metrics:
        metric_status, metric_evidence = _verify_metric(metric, schema, dialect)
        claims.append(
            ClaimVerification(
                subject=f"metric:{metric.id}",
                subject_kind="metric",
                status=metric_status,
                evidence_ids=[item.evidence_id for item in metric_evidence],
            )
        )

    for join in document.relationships:
        join_status, join_evidence = _verify_join(join, schema)
        claims.append(
            ClaimVerification(
                subject=f"join:{join.child_table}->{join.parent_table}",
                subject_kind="relationship",
                status=join_status,
                evidence_ids=[item.evidence_id for item in join_evidence],
            )
        )

    summary: dict[str, int] = {}
    for claim in claims:
        summary[claim.status] = summary.get(claim.status, 0) + 1
    return VerificationReport(
        db_id=document.source_db_id,
        catalog_version=document.catalog_version,
        live_schema_fingerprint=live_fingerprint,
        fingerprint_status=fingerprint_status,
        generated_at=_now(),
        claims=claims,
        summary=summary,
    )
