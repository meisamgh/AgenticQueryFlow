"""Compact graph/KPI lookup over approved catalog facts, not a second registry."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from semantic_text2sql.models import ApprovedMetricDefinition, SchemaInfo, SemanticContract
from semantic_text2sql.semantic_catalog import (
    CatalogDocument,
    MetricCatalogEntry,
    schema_fingerprint,
)


def compact_grounding(
    metrics: list[dict[str, Any]], joins: dict[str, Any] | None, *, max_chars: int = 12000
) -> str:
    """Omit approval notes, retain required facts; never truncate an approved formula."""
    keep = {
        "id",
        "label",
        "description",
        "formula",
        "source_relation",
        "required_fields",
        "mandatory_filters",
        "dimensions",
        "valid_time_grains",
        "unit",
        "aggregation",
        "version",
        "dependencies",
        "catalog_version",
        "provenance",
        "grain",
        "time",
        "required_joins",
        "format",
        "missing_data_policy",
        "rollup",
    }
    payload_metrics = [
        {key: value for key, value in metric.items() if key in keep} for metric in metrics
    ]
    for metric in payload_metrics:
        metric["required_joins"] = [
            {key: value for key, value in join.items() if key != "approval"}
            for join in metric.get("required_joins", [])
        ]
        if metric.get("grain"):
            metric["grain"] = {
                key: value for key, value in metric["grain"].items() if key != "approval"
            }
    payload_joins = dict(joins) if joins else None
    if payload_joins:
        payload_joins["relationships"] = [
            {
                **edge,
                "facts": {key: value for key, value in edge["facts"].items() if key != "approval"},
            }
            for edge in payload_joins.get("relationships", [])
        ]
    encoded = json.dumps(
        {"metrics": payload_metrics, "joins": payload_joins}, separators=(",", ":")
    )
    if len(encoded) > max_chars:
        raise ValueError("Required semantic grounding exceeds the bounded context size.")
    return encoded


def resolve_metrics(
    question: str,
    schema: SchemaInfo,
    document: CatalogDocument | None,
    contract: SemanticContract,
    *,
    limit: int = 4,
    metric_ids: list[str] | None = None,
    strict_dependencies: bool = False,
) -> tuple[SemanticContract, list[dict[str, Any]]]:
    """Only explicit approved IDs/labels; ambiguous matches never choose a formula."""

    def unavailable() -> tuple[SemanticContract, list[dict[str, Any]]]:
        named_approved = bool(document and any(
            metric.status == "approved" and any(
                re.search(r"(?<!\w)" + re.escape(label.casefold()) + r"(?!\w)", question.casefold())
                for label in (metric.id, metric.label)) for metric in document.metrics))
        if contract.approved_metrics or named_approved:
            raise ValueError(
                "The accepted approved metric no longer has a current catalog definition."
            )
        return contract, []

    if document is None or document.source_db_id != schema.db_id:
        return unavailable()
    if document.metadata_status in {"stale", "conflicting"}:
        return unavailable()
    if document.schema_fingerprint and document.schema_fingerprint != schema_fingerprint(schema):
        return unavailable()
    columns = {table.name: {col.name for col in table.columns} for table in schema.tables}
    metrics = {metric.id: metric for metric in document.metrics}
    requested = {metric.id for metric in contract.approved_metrics} | set(metric_ids or [])
    roots = (set(metric_ids or []) | set(
        contract.kpi_grounding.resolution.selected_ids if contract.kpi_grounding
        else [metric.id for metric in contract.approved_metrics]))
    text = " ".join(re.findall(r"\w+", question.casefold()))
    matches = [
        metric
        for metric in document.metrics
        if (metric.id in roots if metric_ids is not None else metric.id in requested)
        or (metric_ids is None and any(
            re.search(
                r"(?<!\w)" + re.escape(" ".join(re.findall(r"\w+", label.casefold()))) + r"(?!\w)",
                text,
            )
            for label in (metric.label, metric.id)
        ))
    ]
    selected: dict[str, MetricCatalogEntry] = {}
    failures: list[str] = []

    def reject(reason: str, metric: MetricCatalogEntry) -> bool:
        failures.append(f"{reason}:{metric.id}")
        return False

    def visit(metric: MetricCatalogEntry, pending: set[str]) -> bool:
        if metric.id in pending:
            return reject("KPI_DEPENDENCY_CYCLE", metric)
        if len(pending) >= 16:
            return reject("KPI_DEPENDENCY_LIMIT", metric)
        if metric.status != "approved":
            return reject("KPI_DEPENDENCY_UNAPPROVED", metric)
        if metric.id in selected:
            return True
        if len(selected) >= 16:
            return reject("KPI_DEPENDENCY_LIMIT", metric)
        fields = columns.get(metric.source_relation, set())
        if not set(metric.required_fields) <= fields:
            return reject("KPI_SCHEMA_DRIFT", metric)
        try:
            expression = parse_one(metric.formula, read=schema.dialect)
        except ParseError:
            return reject("KPI_FORMULA_PARSE_ERROR", metric)
        if (
            not expression
            or expression.find(exp.Query)
            or any(
                col.name not in fields
                or (
                    col.table
                    and ".".join(part for part in (col.catalog, col.db, col.table) if part)
                    != metric.source_relation
                    and (bool(col.db) or col.table != metric.source_relation.rsplit(".", 1)[-1])
                )
                for col in expression.find_all(exp.Column)
            )
        ):
            return reject("KPI_FORMULA_UNSUPPORTED", metric)
        if not {col.name for col in expression.find_all(exp.Column)} <= set(metric.required_fields):
            return False
        for predicate in metric.mandatory_filters:
            table, _, column = predicate.operand.rpartition(".")
            if column not in columns.get(table, set()):
                return False
        required_relations = {metric.source_relation, *[
            predicate.operand.rsplit(".", 1)[0] for predicate in metric.mandatory_filters]}
        for join in document.relationships:
            if (join.status == "approved"
                    and {join.child_table, join.parent_table} <= required_relations
                    and (join.forbidden
                         or not set(join.child_columns) <= columns.get(join.child_table, set())
                         or not set(join.parent_columns) <= columns.get(join.parent_table, set()))):
                return False
        for dimension in metric.dimensions:
            table, _, column = (
                dimension.rpartition(".")
                if "." in dimension
                else (metric.source_relation, ".", dimension)
            )
            if column not in columns.get(table, set()):
                return False
        for dep in sorted(metric.dependencies):
            if dep not in metrics:
                return reject(f"KPI_DEPENDENCY_MISSING:{dep}", metric)
            if strict_dependencies and metrics[dep].source_relation != metric.source_relation:
                return reject("KPI_CROSS_RELATION_COMPOSITE_UNSUPPORTED", metric)
            if not visit(metrics[dep], pending | {metric.id}):
                return False
        if len(selected) >= 16:
            return reject("KPI_DEPENDENCY_LIMIT", metric)
        selected[metric.id] = metric
        return True

    # Identical labels with conflicting approved formulas are not authoritative matches.
    labels: dict[str, set[str]] = {}
    for metric in matches:
        labels.setdefault(metric.label.casefold(), set()).add(metric.formula)
    if any(len(formulas) > 1 for formulas in labels.values()):
        raise ValueError("The business metric has conflicting catalog definitions; "
                         "review is required.")
    if len(matches) > limit:
        raise ValueError("Too many required metrics for the bounded grounding context.")
    for metric in matches[:limit]:
        before = selected.copy()
        if len(labels[metric.label.casefold()]) > 1 or not visit(metric, set()):
            selected = before
            if metric.status == "approved":
                raise ValueError("The requested approved metric has unsupported schema, "
                                 "dependency or relationship requirements. " + ", ".join(failures))
    facts: list[dict[str, Any]] = []
    definitions = []
    required = list(contract.required_columns)
    filters = list(contract.mandatory_metric_filters)
    for metric in selected.values():
        entity = next(
            entity for entity in document.entities if entity.relation == metric.source_relation
        )
        metric_identity = hashlib.sha256(json.dumps({
            "catalog_version": document.catalog_version,
            "metric": metric.model_dump(mode="json"),
            "entity": entity.model_dump(mode="json"),
            "relationships": [item.model_dump(mode="json")
                              for item in document.relationships
                              if metric.source_relation in
                              {item.child_table, item.parent_table}],
        }, sort_keys=True).encode()).hexdigest()
        required_relations = {
            metric.source_relation,
            *(predicate.operand.rsplit(".", 1)[0] for predicate in metric.mandatory_filters),
        }
        facts.append(
            {
                **metric.model_dump(mode="json"),
                "catalog_version": document.catalog_version,
                "catalog_identity": metric_identity,
                "provenance": "human_approved_semantic_catalog",
                "grain": entity.grain.model_dump(mode="json")
                if entity.grain and entity.grain.status == "approved"
                else None,
                "time": entity.time.model_dump(mode="json")
                if entity.time and entity.status == "approved"
                and entity.time.event_time in columns.get(metric.source_relation, set())
                else None,
                "required_joins": [
                    join.model_dump(mode="json")
                    for join in document.relationships
                    if join.status == "approved"
                    and not join.forbidden
                    and {join.child_table, join.parent_table} <= required_relations
                ],
            }
        )
        definitions.append(
            ApprovedMetricDefinition(
                id=metric.id,
                formula=metric.formula,
                source_relation=metric.source_relation,
                required_fields=metric.required_fields,
                mandatory_filters=metric.mandatory_filters,
                catalog_version=document.catalog_version,
                metric_version=metric.version,
                unit=metric.unit,
                catalog_identity=metric_identity,
            )
        )
        required.extend(f"{metric.source_relation}.{field}" for field in metric.required_fields)
        required.extend(predicate.operand for predicate in metric.mandatory_filters)
        filters.extend(metric.mandatory_filters)
    for approved_filter in filters:
        if any(
            existing.operand.casefold() == approved_filter.operand.casefold()
            and ((existing.operator == approved_filter.operator == "="
                  and existing.value != approved_filter.value)
                 or (existing.operator == "IN" and approved_filter.operator == "="
                     and isinstance(existing.value, list)
                     and approved_filter.value not in existing.value)
                 or (existing.operator == "=" and approved_filter.operator == "IN"
                     and isinstance(approved_filter.value, list)
                     and existing.value not in approved_filter.value)
                 or ({existing.operator, approved_filter.operator} == {"=", "!="}
                     and existing.value == approved_filter.value))
            for existing in [*contract.filters, *filters]
        ):
            raise ValueError("The requested filter conflicts with the approved metric definition.")
    # Do not trust incoming approved-metric labels over a conflicting catalog definition.
    if requested - selected.keys():
        raise ValueError(
            "An explicitly requested approved metric is stale, unavailable or conflicting."
        )
    for previous in contract.approved_metrics:
        current = selected[previous.id]
        resolved = next(item for item in definitions if item.id == previous.id)
        if (
            previous.formula != current.formula
            or (previous.catalog_identity is not None
                and previous.catalog_identity != resolved.catalog_identity)
            or set(previous.required_fields) != set(current.required_fields)
            or previous.source_relation != current.source_relation
            or {item.model_dump_json() for item in previous.mandatory_filters}
            != {item.model_dump_json() for item in current.mandatory_filters}
        ):
            raise ValueError("The accepted metric definition changed; review the current catalog.")
    return contract.model_copy(
        update={
            "approved_metrics": definitions,
            "required_columns": list(dict.fromkeys(required)),
            "mandatory_metric_filters": list(
                {item.model_dump_json(): item for item in filters}.values()
            ),
        }
    ), facts
