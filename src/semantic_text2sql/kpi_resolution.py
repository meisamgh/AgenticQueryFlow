"""Bounded catalog-backed KPI identity; retrieval suggests, approval governs."""
from __future__ import annotations

import re
from collections import deque
from contextlib import suppress

from semantic_text2sql.glossary import BusinessGlossary
from semantic_text2sql.hybrid_retrieval import DenseEncoder, _bm25, _dense_scores, _ranks, _rrf
from semantic_text2sql.models import (
    KPICandidate,
    KPIGrounding,
    KPIMatchMethod,
    KPIResolution,
    SchemaInfo,
    SemanticContract,
)
from semantic_text2sql.semantic_catalog import CatalogDocument, schema_fingerprint


def normalized(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold()))


def resolve_kpi_candidates(
    question: str, schema: SchemaInfo, document: CatalogDocument | None,
    *, glossary: BusinessGlossary | None = None, encoder: DenseEncoder | None = None,
) -> KPIResolution:
    if document is None or document.source_db_id != schema.db_id:
        return KPIResolution()
    if len(document.metrics) > 256:
        return KPIResolution(status="UNAVAILABLE", reasons=["KPI_CATALOG_LIMIT"])
    current = (document.metadata_status not in {"stale", "conflicting"}
               and (not document.schema_fingerprint
                    or document.schema_fingerprint == schema_fingerprint(schema)))
    metrics = sorted((m for m in document.metrics if m.status == "approved"), key=lambda m: m.id)
    text = normalized(question[:4000])
    hits: list[tuple[int, int, KPICandidate]] = []
    for metric in metrics:
        phrases: list[tuple[str, KPIMatchMethod, str]] = [
                   (metric.id, "exact_id", "semantic_catalog"),
                   (metric.label, "exact_label", "semantic_catalog"),
                   *[(alias, "approved_alias", "semantic_catalog") for alias in metric.aliases]]
        if glossary and glossary.db_id == schema.db_id:
            anchors = {normalized(metric.id), normalized(metric.label),
                       *(normalized(alias) for alias in metric.aliases)}
            for term in glossary.terms:
                if normalized(term.term) in anchors:
                    phrases.extend((alias, "glossary_alias", f"glossary:{glossary.version}")
                                   for alias in term.synonyms)
        if len(phrases) > 66:
            return KPIResolution(status="UNAVAILABLE", reasons=["KPI_ALIAS_LIMIT"])
        seen_phrases = set()
        for phrase, method, source in phrases:
            phrase = normalized(phrase)
            if not phrase or phrase in seen_phrases:
                continue
            seen_phrases.add(phrase)
            for match in re.finditer(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text):
                if len(hits) >= 256:
                    return KPIResolution(status="UNAVAILABLE", reasons=["KPI_MATCH_LIMIT"])
                hits.append((match.start(), match.end(), KPICandidate(
                    metric_id=metric.id, method=method, source=source,
                    evidence=[f"matched phrase: {phrase}", "human-approved metric definition"],
                    score=1, authoritative_identity=current)))
    # A longer explicit phrase dominates a nested, less specific alias.
    hits = [hit for hit in hits if not any(
        start <= hit[0] and end >= hit[1] and end - start > hit[1] - hit[0]
        for start, end, _ in hits)]
    by_span: dict[tuple[int, int], set[str]] = {}
    candidates: dict[str, KPICandidate] = {}
    for start, end, candidate in hits:
        by_span.setdefault((start, end), set()).add(candidate.metric_id)
        candidates.setdefault(candidate.metric_id, candidate)
    if candidates:
        ambiguous = any(len(ids) > 1 for ids in by_span.values())
        return KPIResolution(
            status="UNAVAILABLE" if not current else "AMBIGUOUS" if ambiguous else "RESOLVED",
            selected_ids=sorted(candidates) if current and not ambiguous else [],
            candidates=list(candidates.values()), catalog_version=document.catalog_version,
            reasons=["STALE_CATALOG"] if not current else ["ALIAS_COLLISION"] if ambiguous else [],
        )
    if not metrics or not current:
        return KPIResolution(catalog_version=document.catalog_version)
    # Reuse schema retrieval's encoder/rank fusion. Scores are candidate evidence,
    # never confidence that an expression has the approved metric's meaning.
    documents = {m.id: " ".join([m.label, *m.aliases, m.description[:1500]]) for m in metrics}
    lexical = _ranks(_bm25(text, documents), positive_only=True)
    dense: dict[str, int] = {}
    if encoder is not None:
        with suppress(Exception):  # Same optional encoder fallback as schema retrieval.
            dense = _ranks(_dense_scores(text, documents, encoder), positive_only=False)
    fused = _rrf((lexical, dense), 60)
    suggestions = [KPICandidate(
        metric_id=key, method="retrieval", source="semantic_catalog_candidate",
        score=fused[key], evidence=[f"BM25 rank: {lexical.get(key)}",
                                    f"embedding rank: {dense.get(key)}",
                                    "similarity is not identity proof"],
    ) for key in sorted(fused, key=lambda key: (-fused[key], key))[:4]]
    return KPIResolution(status="CANDIDATES" if suggestions else "NONE", candidates=suggestions,
                         catalog_version=document.catalog_version)


def build_kpi_grounding(
    resolution: KPIResolution, contract: SemanticContract, document: CatalogDocument,
    question: str, *, schema: SchemaInfo | None = None,
) -> KPIGrounding:
    metrics = {m.id: m for m in document.metrics}
    approved = {m.id for m in contract.approved_metrics}
    distances: dict[str, int] = {}
    queue = deque((key, 0) for key in resolution.selected_ids)
    while queue:
        key, distance = queue.popleft()
        if key in distances:
            continue
        if key not in approved or distance >= 16 or len(distances) >= 16:
            raise ValueError("KPI_DEPENDENCY_UNRESOLVED: no complete approved closure.")
        distances[key] = distance
        queue.extend((dep, distance + 1) for dep in sorted(metrics[key].dependencies))
    supported = {key: [col if "." in col else f"{metrics[key].source_relation}.{col}"
                       for col in metrics[key].dimensions] for key in sorted(approved)}
    text = normalized(question)
    requested = set(contract.grain) | set(contract.output_grain) | set(contract.outputs)
    requested_grains = {normalized(item).removesuffix("s")
                        for item in [*contract.grain, *contract.output_grain]}
    grouping = re.search(r"\b(?:by|per)\s+(.+)", text)
    # A typed accepted contract owns grouping; merely mentioning a supported
    # dimension (e.g. a filter) must not introduce a new GROUP BY.
    grouping_text = (grouping.group(1) if grouping and not contract.original_question
                     and not contract.resolved_question else "")
    supported_requested = (set.intersection(*[
        set(supported.get(key, [])) for key in resolution.selected_ids
    ]) if resolution.selected_ids else set())
    for column in [*contract.grain, *contract.output_grain]:
        if "." in column and column not in supported_requested:
            raise ValueError("KPI_DIMENSION_UNSUPPORTED: " + column)
    dimensions = set()
    for column in sorted({col for cols in supported.values() for col in cols}):
        table, _, name = column.rpartition(".")
        aliases = {normalized(column), normalized(table).removesuffix("s")}
        if name.casefold() not in {"name", "id", "date", "value"}:
            aliases.add(normalized(name))
        if (column in requested or aliases & requested_grains or any(
            re.search(r"(?<!\w)" + re.escape(alias) + r"s?(?!\w)", grouping_text)
            for alias in aliases if alias
        )):
            dimensions.add(column)
    if not dimensions <= supported_requested:
        raise ValueError("KPI_DIMENSION_UNSUPPORTED: no shared approved dimensional grain.")
    live_columns = ({f"{table.name}.{column.name}" for table in schema.tables
                     for column in table.columns} if schema else {
                         f"{entity.relation}.{column}" for entity in document.entities
                         for column in entity.columns})
    filter_columns = []
    for predicate in [*contract.filters, *contract.temporal_boundaries]:
        if predicate.operand in live_columns:
            filter_columns.append(predicate.operand)
        elif re.fullmatch(r"\w+(?:\.\w+)+", predicate.operand):
            raise ValueError("KPI_FILTER_COLUMN_UNAVAILABLE: " + predicate.operand)
    output_columns = []
    for output in contract.outputs:
        if output in live_columns:
            output_columns.append(output)
        elif re.fullmatch(r"\w+(?:\.\w+)+", output):
            raise ValueError("KPI_OUTPUT_COLUMN_UNAVAILABLE: " + output)
    columns = list(dict.fromkeys([*contract.required_columns, *filter_columns, *output_columns,
                                 *sorted(dimensions)]))
    return KPIGrounding(
        resolution=resolution,
        dependency_chain={key: list(metrics[key].dependencies) for key in sorted(approved)},
        dependency_distances=distances, mandatory_columns=columns,
        source_relations={key: metrics[key].source_relation for key in sorted(approved)},
        source_grains={entity.relation: list(entity.grain.keys) for entity in document.entities
                       if entity.grain and entity.grain.status == "approved"
                       and entity.relation in {metrics[key].source_relation for key in approved}},
        aggregation_rules={key: metrics[key].aggregation for key in sorted(approved)},
        rollup_rules={key: metrics[key].rollup for key in sorted(approved)},
        mandatory_tables=sorted({col.rsplit(".", 1)[0] for col in columns}
                                | {m.source_relation for m in contract.approved_metrics}),
        requested_dimensions=sorted(dimensions), supported_dimensions=supported,
        valid_time_grains={key: list(metrics[key].valid_time_grains) for key in sorted(approved)},
        catalog_version=document.catalog_version,
    )
