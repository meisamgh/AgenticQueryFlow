"""KPI-first acceptance: governed identity, closure, graph, SQL and ML boundaries."""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_semantic_catalog import APPROVAL, _registry
from test_semantic_graph import approved_document, join_schema

from semantic_text2sql.agent import TextToSQLAgent
from semantic_text2sql.conversation import resolve_semantic_contract
from semantic_text2sql.glossary import BusinessGlossary, GlossaryStore
from semantic_text2sql.graph_grounding import resolve_metrics
from semantic_text2sql.historical import HistoricalQueryStore
from semantic_text2sql.hybrid_retrieval import (
    KPI_RERANKER_FEATURES_V2,
    RERANKER_FEATURES,
    HybridSchemaRetriever,
    LightGBMSchemaReranker,
    kpi_candidate_features,
)
from semantic_text2sql.join_resolver import resolve_joins
from semantic_text2sql.kpi_resolution import build_kpi_grounding, resolve_kpi_candidates
from semantic_text2sql.models import RequestedChange, SchemaInfo, SemanticContract, SemanticFilter
from semantic_text2sql.profiling import ProfileStore
from semantic_text2sql.semantic_catalog import (
    CatalogDocument,
    ColumnCatalogEntry,
    JoinCatalogEntry,
    QualityContractEntry,
    SemanticCatalogStore,
    TableCatalogEntry,
)
from semantic_text2sql.semantic_graph import SemanticGraphStore, compile_graph
from semantic_text2sql.service import TextToSQLService
from semantic_text2sql.validator import validate_sql_semantics as validate_semantic_sql


class Encoder:
    def encode(self, texts: Any) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


class SQLModel:
    def __init__(self, sql: str) -> None:
        self.sql, self.calls, self.context = sql, 0, ""

    async def generate(self, **kwargs: Any) -> tuple[str, int]:
        self.calls += 1
        self.context = kwargs["profile_context"]
        return self.sql, 1


def fixture(tmp_path: Path) -> tuple[Any, CatalogDocument]:
    registry = _registry(tmp_path, extra_sql=[
        "ALTER TABLE orders ADD COLUMN refund_amount REAL NOT NULL DEFAULT 0",
        "UPDATE orders SET refund_amount=amount*0.1",
        "CREATE TABLE customers (id INTEGER PRIMARY KEY, department_id INTEGER)",
        "INSERT INTO customers VALUES (1,10),(2,20)",
        "CREATE TABLE departments (id INTEGER PRIMARY KEY, name TEXT)",
        "INSERT INTO departments VALUES (10,'A'),(20,'B')",
        "CREATE TABLE unrelated (id INTEGER PRIMARY KEY, amount REAL)",
    ])
    document = approved_document()
    orders = document.entities[0]
    orders.columns["refund_amount"] = ColumnCatalogEntry(role="measure")
    orders.quality.positive.append("refund_amount")
    for name, columns in [("customers", ["id", "department_id"]),
                          ("departments", ["id", "name"])]:
        document.entities.append(TableCatalogEntry(
            id=name, relation=name, description=name,
            columns={col: ColumnCatalogEntry(role="dimension") for col in columns},
            quality=QualityContractEntry(required=columns),
        ))
    base = document.metrics[0]
    gross = base.model_copy(update={"id": "gross_sales", "label": "Gross sales"})
    refunds = base.model_copy(update={"id": "refunds", "label": "Refunds",
                                      "formula": "SUM(refund_amount)",
                                      "required_fields": ["refund_amount"]})
    net = base.model_copy(update={
        "id": "net_revenue", "label": "Net revenue", "aliases": ["sales after refunds"],
        "formula": "SUM(amount) - SUM(refund_amount)",
        "required_fields": ["amount", "refund_amount"],
        "dependencies": ["gross_sales", "refunds"], "dimensions": ["departments.name"],
        "mandatory_filters": [SemanticFilter(operand="orders.status", operator="=",
                                              value="complete")],
    })
    document.metrics = [net, gross, refunds]
    document.relationships = [JoinCatalogEntry(
        child_table=a, child_columns=[ak], parent_table=b, parent_columns=[bk],
        source="manual", cardinality="many_to_one", status="approved", approval=APPROVAL,
    ) for a, ak, b, bk in [("orders", "customer_id", "customers", "id"),
                           ("customers", "department_id", "departments", "id")]]
    return registry, CatalogDocument.model_validate(document.model_dump())


def service(tmp_path: Path, registry: Any, document: CatalogDocument,
            model: SQLModel) -> TextToSQLService:
    root = tmp_path / "catalog"
    root.mkdir(exist_ok=True)
    (root / "shop.json").write_text(document.model_dump_json())
    return TextToSQLService(
        database=registry, profiles=ProfileStore(tmp_path / "profiles"), glossaries=GlossaryStore(),
        history=HistoricalQueryStore(), retriever=HybridSchemaRetriever(Encoder(), max_tables=1,
                                                                      max_columns_per_table=1),
        agent=TextToSQLAgent(registry, model), context_completers={},
        semantic_catalogs=SemanticCatalogStore(root),
        graph_store=SemanticGraphStore(tmp_path / "graphs", persistence_enabled=False),
    )


def execute(svc: TextToSQLService, question: str = "Sales after refunds by department") -> Any:
    return asyncio.run(svc.execute_question(question=question, db_id="shop", provider="groq",
                                           model="scripted", max_attempts=1))


@pytest.mark.parametrize("question,method", [("net_revenue", "exact_id"),
                                            ("Net revenue", "exact_label"),
                                            ("Sales after refunds", "approved_alias")])
def test_identity(question: str, method: str, tmp_path: Path) -> None:
    registry, document = fixture(tmp_path)
    result = resolve_kpi_candidates(question, registry.inspect("shop"), document)
    assert result.status == "RESOLVED" and result.selected_ids == ["net_revenue"]
    assert result.candidates[0].method == method
    assert result.candidates[0].authoritative_identity


def test_glossary_alias_retrieval_candidates_and_ambiguity(tmp_path: Path) -> None:
    registry, document = fixture(tmp_path)
    schema = registry.inspect("shop")
    glossary = BusinessGlossary(db_id="shop", version="1", source="curated", precedence=[],
                               terms=[{"term": "Net revenue", "definition": "Approved metric",
                                       "synonyms": ["earnings after returns"]}])
    result = resolve_kpi_candidates("earnings after returns", schema, document, glossary=glossary)
    assert result.status == "RESOLVED" and result.candidates[0].method == "glossary_alias"
    candidates = resolve_kpi_candidates("amounts sum", schema, document, encoder=Encoder())
    assert candidates.status == "CANDIDATES" and candidates.selected_ids == []
    assert not any(item.authoritative_identity for item in candidates.candidates)
    document.metrics[1].aliases = ["sales after refunds"]
    assert resolve_kpi_candidates("sales after refunds", schema, document).status == "AMBIGUOUS"
    document.metrics[0].status, document.metrics[0].approval = "inferred", None
    assert resolve_kpi_candidates("net_revenue", schema, document).selected_ids == []
    document.source_db_id = "other"
    assert resolve_kpi_candidates("gross_sales", schema, document).status == "NONE"


@pytest.mark.parametrize("failure,code", [("cycle", "CYCLE"), ("missing", "MISSING"),
                                          ("unapproved", "UNAPPROVED"),
                                          ("formula", "FORMULA_UNSUPPORTED")])
def test_invalid_dependencies_are_explicit(tmp_path: Path, failure: str, code: str) -> None:
    registry, document = fixture(tmp_path)
    if failure == "cycle":
        document.metrics[1].dependencies = ["net_revenue"]
    elif failure == "missing":
        document.metrics[0].dependencies.append("unknown")
    elif failure == "unapproved":
        document.metrics[1].status, document.metrics[1].approval = "inferred", None
    else:
        document.metrics[0].formula = "gross_sales - refunds"
    with pytest.raises(ValueError, match=code):
        resolve_metrics("net_revenue", registry.inspect("shop"), document, SemanticContract())


def test_dependency_first_sql_generation_real_sqlite(tmp_path: Path, monkeypatch: Any) -> None:
    registry, document = fixture(tmp_path)
    monkeypatch.setenv("TEXT2SQL_KPI_FIRST_ENABLED", "true")
    monkeypatch.setenv("TEXT2SQL_GRAPH_ENABLED", "true")
    model = SQLModel("SELECT d.name, SUM(o.amount)-SUM(o.refund_amount) AS net_revenue "
                     "FROM orders o JOIN customers c ON o.customer_id=c.id "
                     "JOIN departments d ON c.department_id=d.id "
                     "WHERE o.status='complete' GROUP BY d.name")
    result = execute(service(tmp_path, registry, document, model))
    assert result.generated.accepted, result.generated.model_error
    assert result.generated.rows == [["A", 135.0]]
    assert model.calls == 1
    grounding = result.semantic_contract.kpi_grounding
    assert grounding and grounding.join_status == "CONNECTED"
    assert set(grounding.dependency_chain) == {"net_revenue", "gross_sales", "refunds"}
    assert grounding.dependency_distances["gross_sales"] == 1
    assert {"orders", "customers", "departments"} == set(result.approved_tables)
    assert {"orders.amount", "orders.refund_amount", "orders.status", "departments.name"} <= set(
        result.semantic_contract.required_columns)
    assert "SUM(amount) - SUM(refund_amount)" in model.context
    assert "kpi_grounding" in model.context and "complete" in model.context
    assert not result.generated.telemetry.planner_call_used


@pytest.mark.parametrize("graph,flag", [(True, False), (False, True)])
def test_opt_in_and_graph_off_compatibility(tmp_path: Path, monkeypatch: Any,
                                          graph: bool, flag: bool) -> None:
    registry, document = fixture(tmp_path)
    monkeypatch.setenv("TEXT2SQL_GRAPH_ENABLED", str(graph).lower())
    monkeypatch.setenv("TEXT2SQL_KPI_FIRST_ENABLED", str(flag).lower())
    model = SQLModel("SELECT SUM(amount) FROM orders")
    result = execute(service(tmp_path, registry, document, model), "Order amounts")
    assert result.semantic_contract.kpi_grounding is None
    assert model.calls == 1


@pytest.mark.parametrize("status", ["DISCONNECTED", "AMBIGUOUS", "BUDGET_EXHAUSTED"])
def test_unresolved_mandatory_join_stops_before_model(tmp_path: Path, monkeypatch: Any,
                                                    status: str) -> None:
    from semantic_text2sql.join_resolver import JoinResolution
    registry, document = fixture(tmp_path)
    monkeypatch.setenv("TEXT2SQL_KPI_FIRST_ENABLED", "true")
    monkeypatch.setattr("semantic_text2sql.service.resolve_joins", lambda *a, **kw: JoinResolution(
        status=status, tables=[], explanation="Bounded test"))
    model = SQLModel("SELECT 1")
    result = execute(service(tmp_path, registry, document, model))
    assert not result.generated.accepted and model.calls == 0
    assert status in result.generated.model_error


def grounded(tmp_path: Path) -> tuple[SchemaInfo, SemanticContract]:
    registry, document = fixture(tmp_path)
    schema = registry.inspect("shop")
    resolution = resolve_kpi_candidates("sales after refunds by department", schema, document)
    contract, _ = resolve_metrics("", schema, document, SemanticContract(),
                                  metric_ids=resolution.selected_ids)
    grounding = build_kpi_grounding(resolution, contract, document, "by department")
    return schema, contract.model_copy(update={"kpi_grounding": grounding})


def test_required_filters_columns_and_grain_validation(tmp_path: Path) -> None:
    _, contract = grounded(tmp_path)
    findings = validate_semantic_sql("SELECT SUM(amount) FROM orders", contract)
    assert any(item.status == "FAIL" and item.code == "KPI_REQUIRED_COLUMN_MISSING"
               for item in findings)
    assert any(item.status == "FAIL" and "FILTER" in item.code for item in findings)
    contract.grain = ["year"]
    assert any(item.code == "KPI_TIME_GRAIN_UNSUPPORTED" and item.status == "UNRESOLVED"
               for item in validate_semantic_sql("SELECT SUM(amount) FROM orders", contract))


def test_v1_features_unchanged_and_v2_is_pre_join_only(tmp_path: Path) -> None:
    schema, contract = grounded(tmp_path)
    assert len(RERANKER_FEATURES) == 11
    assert KPI_RERANKER_FEATURES_V2[:11] == RERANKER_FEATURES
    assert "estimated_join_count" not in KPI_RERANKER_FEATURES_V2
    grounding = contract.kpi_grounding
    assert grounding
    vector = kpi_candidate_features("column:orders.refund_amount", schema, grounding)
    assert vector[:3] == [1, 1, 0]
    assert kpi_candidate_features("column:unrelated.amount", schema, grounding)[0] == 0


def test_composite_and_ambiguous_graph_paths_preserved() -> None:
    schema = join_schema([("a", "b"), ("b", "c"), ("a", "d"), ("d", "c")])
    assert resolve_joins(["a", "c"], schema, snapshot=compile_graph(schema)).status == "AMBIGUOUS"


def test_pin_survives_low_ranking_and_unrelated_tables_are_pruned(tmp_path: Path) -> None:
    schema, contract = grounded(tmp_path)
    grounding = contract.kpi_grounding
    assert grounding

    class BadRanker:
        def score(self, features: Any) -> list[float]:
            return [-1000.0] * len(features)

    retriever = HybridSchemaRetriever(Encoder(), max_tables=1, max_columns_per_table=1,
                                      reranker=BadRanker())
    _, selection, _ = retriever.retrieve(
        "sales after refunds by department", None, schema, None, None,
        mandatory_columns=grounding.mandatory_columns,
        mandatory_tables=grounding.mandatory_tables, defer_join_expansion=True,
    )
    assert set(selection.tables) == {"orders", "departments"}
    assert {"amount", "refund_amount", "status"} <= set(selection.columns["orders"])
    assert "name" in selection.columns["departments"]


@pytest.mark.parametrize("change", ["stale", "fingerprint", "version", "conflicting"])
def test_stale_alias_and_accepted_definition_drift(tmp_path: Path, change: str) -> None:
    registry, document = fixture(tmp_path)
    schema = registry.inspect("shop")
    accepted, _ = resolve_metrics("net_revenue", schema, document, SemanticContract())
    if change == "stale":
        document.metadata_status = "stale"
    elif change == "conflicting":
        document.metadata_status = "conflicting"
    elif change == "fingerprint":
        document.schema_fingerprint = "old"
    else:
        document.metrics[0].version += 1
        with pytest.raises(ValueError, match="changed"):
            resolve_metrics("net_revenue", schema, document, accepted)
        return
    assert resolve_kpi_candidates("sales after refunds", schema, document).status == "UNAVAILABLE"


def test_cross_relation_metric_dependency_is_explicitly_unsupported(tmp_path: Path) -> None:
    registry, document = fixture(tmp_path)
    document.metrics[2].source_relation = "unrelated"
    document.metrics[2].required_fields = ["amount"]
    document.metrics[2].formula = "SUM(amount)"
    with pytest.raises(ValueError, match="CROSS_RELATION_COMPOSITE_UNSUPPORTED"):
        resolve_metrics("net_revenue", registry.inspect("shop"), document, SemanticContract(),
                        strict_dependencies=True)


def test_ratio_mismatch_is_not_passed_as_an_approved_formula(tmp_path: Path) -> None:
    registry, document = fixture(tmp_path)
    metric = document.metrics[0]
    metric.formula = "SUM(amount) / SUM(refund_amount)"
    schema = registry.inspect("shop")
    resolution = resolve_kpi_candidates("net_revenue", schema, document)
    contract, _ = resolve_metrics("net_revenue", schema, document, SemanticContract())
    contract.kpi_grounding = build_kpi_grounding(resolution, contract, document, "net_revenue")
    wrong = validate_semantic_sql("SELECT SUM(refund_amount)/SUM(amount) FROM orders", contract)
    assert any(item.status == "FAIL" and item.code == "APPROVED_METRIC_FORMULA_MISMATCH"
               for item in wrong)
    valid = validate_semantic_sql("SELECT SUM(amount)/SUM(refund_amount) FROM orders", contract)
    assert any(item.status == "PASS" and item.code == "APPROVED_METRIC_FORMULA_MATCH"
               for item in valid)
    rewritten = validate_semantic_sql(
        "WITH x AS (SELECT SUM(amount) AS a, SUM(refund_amount) AS b FROM orders) "
        "SELECT a / NULLIF(b,0) FROM x", contract)
    assert any(item.status == "UNRESOLVED" for item in rewritten)


def test_composite_join_missing_key_fails_and_fanout_stays_unresolved(tmp_path: Path) -> None:
    _, contract = grounded(tmp_path)
    grounding = contract.kpi_grounding
    assert grounding
    grounding.required_relationships = [{
        "child_table": "orders", "parent_table": "customers",
        "child_columns": ["customer_id", "tenant_id"], "parent_columns": ["id", "tenant_id"],
        "cardinality": "many_to_many", "provenance": "approved_catalog",
    }]
    findings = validate_semantic_sql(
        "SELECT SUM(o.amount) FROM orders o JOIN customers c ON o.customer_id=c.id", contract)
    assert any(item.code == "KPI_JOIN_KEYS_INCOMPLETE" and item.status == "FAIL"
               for item in findings)
    findings = validate_semantic_sql(
        "SELECT SUM(o.amount) FROM orders o JOIN customers c "
        "ON o.customer_id=c.id AND o.tenant_id=c.tenant_id", contract)
    assert not any(item.code == "KPI_JOIN_KEYS_INCOMPLETE" for item in findings)
    assert any(item.code == "KPI_JOIN_GRAIN_UNPROVEN" and item.status == "UNRESOLVED"
               for item in findings)


def test_missing_catalog_and_inferred_catalog_do_not_create_kpis(tmp_path: Path) -> None:
    registry, document = fixture(tmp_path)
    schema = registry.inspect("shop")
    assert resolve_kpi_candidates("sales after refunds", schema, None).selected_ids == []
    for metric in document.metrics:
        metric.status, metric.approval = "inferred", None
    assert resolve_kpi_candidates("sales after refunds", schema, document).status == "NONE"


def test_conflicting_filter_does_not_override_mandatory_kpi_filter(tmp_path: Path) -> None:
    registry, document = fixture(tmp_path)
    contract = SemanticContract(filters=[SemanticFilter(operand="orders.status", operator="=",
                                                        value="pending")])
    with pytest.raises(ValueError, match="conflicts"):
        resolve_metrics("net_revenue", registry.inspect("shop"), document, contract)


def test_invalid_requested_dimension_and_conflicting_dependency_filters(tmp_path: Path) -> None:
    registry, document = fixture(tmp_path)
    schema = registry.inspect("shop")
    resolution = resolve_kpi_candidates("net_revenue", schema, document)
    contract, _ = resolve_metrics("net_revenue", schema, document,
                                  SemanticContract(grain=["orders.status"]))
    with pytest.raises(ValueError, match="DIMENSION_UNSUPPORTED"):
        build_kpi_grounding(resolution, contract, document, "by status")
    document.metrics[1].mandatory_filters = [SemanticFilter(
        operand="orders.status", operator="=", value="pending")]
    with pytest.raises(ValueError, match="conflicts"):
        resolve_metrics("net_revenue", schema, document, SemanticContract())


def test_v2_artifact_requires_matching_manifest_and_feature_names(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    path = tmp_path / "ranker.txt"
    path.write_text("scripted model; not a trained artifact")
    names = list(RERANKER_FEATURES)
    booster = SimpleNamespace(num_feature=lambda: len(names), feature_name=lambda: names,
                              predict=lambda vectors: [0.0] * len(vectors))
    monkeypatch.setitem(sys.modules, "lightgbm", SimpleNamespace(Booster=lambda **kw: booster))
    assert LightGBMSchemaReranker(path).feature_schema == "v1"
    with pytest.raises(FileNotFoundError):
        LightGBMSchemaReranker(path, feature_schema="kpi_v2")
    path.with_suffix(".features.json").write_text(json.dumps({
        "schema": "kpi_v2", "features": list(KPI_RERANKER_FEATURES_V2),
    }))
    with pytest.raises(ValueError, match="feature names"):
        LightGBMSchemaReranker(path, feature_schema="kpi_v2")
    names[:] = KPI_RERANKER_FEATURES_V2
    assert LightGBMSchemaReranker(path, feature_schema="kpi_v2").score([[0.0] * len(names)]) == [0]


def test_bounded_catalog_and_context_and_determinism(tmp_path: Path) -> None:
    from semantic_text2sql.graph_grounding import compact_grounding

    registry, document = fixture(tmp_path)
    schema = registry.inspect("shop")
    first = resolve_kpi_candidates("Sales after refunds", schema, document)
    assert first == resolve_kpi_candidates("Sales after refunds", schema, document)
    _, facts = resolve_metrics("net_revenue", schema, document, SemanticContract())
    with pytest.raises(ValueError, match="exceeds"):
        compact_grounding(facts, None, max_chars=20)
    document.metrics = [document.metrics[0].model_copy(update={"id": f"metric_{i}"})
                        for i in range(257)]
    assert resolve_kpi_candidates("net revenue", schema, document).reasons == ["KPI_CATALOG_LIMIT"]


def test_dependency_bound_and_followup_revalidates_complete_closure(tmp_path: Path) -> None:
    registry, document = fixture(tmp_path)
    schema = registry.inspect("shop")
    base = document.metrics[1]
    document.metrics = [base.model_copy(update={
        "id": f"k{i}", "label": f"KPI {i}", "dependencies": [f"k{i + 1}"] if i < 16 else [],
    }) for i in range(17)]
    with pytest.raises(ValueError, match="LIMIT"):
        resolve_metrics("k0", schema, document, SemanticContract())
    document.metrics = document.metrics[:6]
    document.metrics[-1].dependencies = []
    resolution = resolve_kpi_candidates("k0", schema, document)
    contract, _ = resolve_metrics("k0", schema, document, SemanticContract(), metric_ids=["k0"])
    contract.kpi_grounding = build_kpi_grounding(resolution, contract, document, "k0")
    repeated, _ = resolve_metrics("again", schema, document, contract, metric_ids=[])
    assert len(repeated.approved_metrics) == 6


def test_wrong_grouping_dimension_fails(tmp_path: Path) -> None:
    _, contract = grounded(tmp_path)
    findings = validate_semantic_sql(
        "SELECT d.name, SUM(o.amount)-SUM(o.refund_amount) AS net_revenue FROM orders o "
        "JOIN departments d ON o.customer_id=d.id WHERE o.status='complete' GROUP BY o.status",
        contract)
    assert any(item.status == "FAIL" and item.code == "GROUPING_DIMENSION_MISMATCH"
               for item in findings)


def test_wide_dependency_closure_includes_root_in_bound(tmp_path: Path) -> None:
    registry, document = fixture(tmp_path)
    base = document.metrics[1]
    leaves = [base.model_copy(update={"id": f"leaf{i}", "label": f"Leaf {i}"})
              for i in range(16)]
    root = base.model_copy(update={"id": "root", "label": "Root metric",
                                   "dependencies": [leaf.id for leaf in leaves]})
    document.metrics = [root, *leaves]
    with pytest.raises(ValueError, match="KPI_DEPENDENCY_LIMIT"):
        resolve_metrics("root", registry.inspect("shop"), document, SemanticContract())


def test_join_key_proof_does_not_force_a_specific_join_strategy(tmp_path: Path) -> None:
    _, contract = grounded(tmp_path)
    grounding = contract.kpi_grounding
    assert grounding
    grounding.required_relationships = [{
        "child_table": "orders", "parent_table": "customers", "child_columns": ["customer_id"],
        "parent_columns": ["id"], "cardinality": "many_to_one", "provenance": "approved_catalog",
    }]
    findings = validate_semantic_sql(
        "SELECT SUM(o.amount) FROM orders o, customers c WHERE o.customer_id=c.id", contract)
    assert not any(item.code == "KPI_JOIN_KEYS_INCOMPLETE" for item in findings)
    findings = validate_semantic_sql(
        "SELECT SUM(o.amount) FROM orders o JOIN customers c "
        "ON CAST(o.customer_id AS TEXT)=CAST(c.id AS TEXT)", contract)
    assert any(item.code == "KPI_JOIN_KEYS_INCOMPLETE" and item.status == "UNRESOLVED"
               for item in findings)
    findings = validate_semantic_sql(
        "SELECT SUM(o.amount) FROM orders o JOIN customers c "
        "ON NOT (o.customer_id=c.id) WHERE o.status='complete'", contract)
    assert any(item.code == "KPI_JOIN_KEYS_INCOMPLETE" and item.status == "UNRESOLVED"
               for item in findings)


def test_nested_aliases_preserve_separately_requested_metrics_and_hit_bounds(
    tmp_path: Path,
) -> None:
    registry, document = fixture(tmp_path)
    schema = registry.inspect("shop")
    result = resolve_kpi_candidates("sales after refunds and refunds", schema, document)
    assert result.selected_ids == ["net_revenue", "refunds"]
    result = resolve_kpi_candidates("refunds " * 300, schema, document)
    assert result.status == "UNAVAILABLE" and result.reasons == ["KPI_MATCH_LIMIT"]


def test_dimension_mention_is_not_an_instruction_to_group(tmp_path: Path) -> None:
    registry, document = fixture(tmp_path)
    schema = registry.inspect("shop")
    resolution = resolve_kpi_candidates("net_revenue", schema, document)
    contract, _ = resolve_metrics("net_revenue", schema, document, SemanticContract())
    assert build_kpi_grounding(resolution, contract, document,
                               "net revenue for departments").requested_dimensions == []
    assert build_kpi_grounding(resolution, contract, document,
                               "net revenue by department").requested_dimensions == [
                                   "departments.name"]
    contract.original_question = "Remove the department grouping"
    assert build_kpi_grounding(resolution, contract, document,
                               "do not group by department").requested_dimensions == []


def test_metric_change_does_not_reintroduce_previous_kpi(tmp_path: Path) -> None:
    registry, document = fixture(tmp_path)
    schema = registry.inspect("shop")
    resolution = resolve_kpi_candidates("net_revenue", schema, document)
    accepted, _ = resolve_metrics("net_revenue", schema, document, SemanticContract())
    accepted.kpi_grounding = build_kpi_grounding(resolution, accepted, document, "net_revenue")
    changed = resolve_semantic_contract(
        accepted, original_question="Use refunds instead", resolved_question="Refunds",
        changes=[RequestedChange(operation="CHANGE_METRIC", target="metric", new_value="refunds")],
    )
    resolved, _ = resolve_metrics("Refunds", schema, document, changed, metric_ids=["refunds"])
    assert {metric.id for metric in resolved.approved_metrics} == {"refunds"}
    assert resolved.mandatory_metric_filters == []
    assert accepted.kpi_grounding.resolution.selected_ids == ["net_revenue"]


@pytest.mark.parametrize("keep_dimension_filter", [False, True])
def test_removing_grouping_drops_only_obsolete_kpi_dimension_dependencies(
    tmp_path: Path, keep_dimension_filter: bool,
) -> None:
    _, accepted = grounded(tmp_path)
    old = accepted.kpi_grounding
    assert old
    accepted.grain = accepted.output_grain = ["departments.name"]
    accepted.outputs = ["departments.name", "net_revenue"]
    accepted.required_columns = [*old.mandatory_columns, "orders.customer_id", "customers.id",
                                 "customers.department_id", "departments.id"]
    old.required_relationships = [{
        "child_table": "customers", "child_columns": ["department_id"],
        "parent_table": "departments", "parent_columns": ["id"],
    }]
    if keep_dimension_filter:
        accepted.filters = [SemanticFilter(operand="departments.name", operator="=", value="A")]
    changed = resolve_semantic_contract(
        accepted, original_question="Remove grouping", resolved_question="Net revenue total",
        changes=[RequestedChange(operation="REMOVE_DIMENSION", target="departments.name")],
    )
    assert changed.grain == changed.output_grain == []
    assert changed.outputs == ["net_revenue"]
    assert ("departments.name" in changed.required_columns) == keep_dimension_filter
    assert "departments.id" not in changed.required_columns
    assert "orders.amount" in changed.required_columns
    assert "orders.refund_amount" in changed.required_columns
    assert changed.kpi_grounding is not None
    assert changed.kpi_grounding.resolution == old.resolution
    assert changed.kpi_grounding.required_relationships == []
    assert changed.kpi_grounding.mandatory_columns == []
    assert accepted.kpi_grounding is not None  # Provisional delta cannot mutate accepted state.


def test_real_sqlite_group_removal_rebuilds_minimal_kpi_context(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    registry, document = fixture(tmp_path)
    monkeypatch.setenv("TEXT2SQL_KPI_FIRST_ENABLED", "true")
    model = SQLModel("SELECT d.name, SUM(o.amount)-SUM(o.refund_amount) AS net_revenue "
                     "FROM orders o JOIN customers c ON o.customer_id=c.id "
                     "JOIN departments d ON c.department_id=d.id "
                     "WHERE o.status='complete' GROUP BY d.name")
    svc = service(tmp_path, registry, document, model)
    first = execute(svc)
    assert first.generated.accepted
    accepted = first.semantic_contract.model_copy(deep=True)
    changed = resolve_semantic_contract(
        accepted, original_question="Remove the department grouping",
        resolved_question="Net revenue total", changes=[RequestedChange(
            operation="REMOVE_DIMENSION", target="departments.name")],
    )
    model.sql = ("SELECT SUM(amount)-SUM(refund_amount) AS net_revenue FROM orders "
                 "WHERE status='complete'")
    result = asyncio.run(svc.execute_question(
        question="Net revenue total", db_id="shop", provider="groq", model="scripted",
        max_attempts=1, semantic_contract=changed,
    ))
    assert result.generated.accepted, result.generated.model_error
    assert result.generated.rows == [[135.0]]
    assert result.approved_tables == ["orders"]
    assert result.semantic_contract.kpi_grounding.requested_dimensions == []
    assert model.calls == 2
    assert first.semantic_contract == accepted


def test_group_change_keeps_root_identity_not_dependency_roots(tmp_path: Path) -> None:
    registry, document = fixture(tmp_path)
    schema = registry.inspect("shop")
    base = document.metrics[1]
    document.metrics = [base.model_copy(update={
        "id": f"k{i}", "label": f"KPI {i}", "dependencies": [f"k{i + 1}"] if i < 5 else [],
    }) for i in range(6)]
    resolution = resolve_kpi_candidates("k0", schema, document)
    contract, _ = resolve_metrics("k0", schema, document, SemanticContract(), metric_ids=["k0"])
    contract.kpi_grounding = build_kpi_grounding(resolution, contract, document, "k0")
    changed = resolve_semantic_contract(
        contract, original_question="Remove grouping", resolved_question="KPI 0 total",
        changes=[RequestedChange(operation="REMOVE_DIMENSION", target="department")],
    )
    result, _ = resolve_metrics("again", schema, document, changed, metric_ids=[])
    assert len(result.approved_metrics) == 6
    assert changed.kpi_grounding.resolution.selected_ids == ["k0"]


def test_user_filter_dependency_is_pinned_before_join_grounding(tmp_path: Path) -> None:
    registry, document = fixture(tmp_path)
    schema = registry.inspect("shop")
    resolution = resolve_kpi_candidates("net_revenue", schema, document)
    contract, _ = resolve_metrics("net_revenue", schema, document, SemanticContract(filters=[
        SemanticFilter(operand="departments.name", operator="=", value="A")]))
    grounding = build_kpi_grounding(resolution, contract, document, "Net revenue for department A")
    assert "departments.name" in grounding.mandatory_columns
    assert "departments" in grounding.mandatory_tables
    assert grounding.requested_dimensions == []


@pytest.mark.parametrize("still_grouped", [False, True])
def test_removed_filter_releases_only_unneeded_column_and_join_dependencies(
    tmp_path: Path, still_grouped: bool,
) -> None:
    _, accepted = grounded(tmp_path)
    accepted.filters = [SemanticFilter(operand="departments.name", operator="=", value="A")]
    accepted.required_columns = [*accepted.kpi_grounding.mandatory_columns, "departments.id",
                                 "customers.department_id"]
    accepted.kpi_grounding.required_relationships = [{
        "child_table": "customers", "child_columns": ["department_id"],
        "parent_table": "departments", "parent_columns": ["id"],
    }]
    if still_grouped:
        accepted.grain = ["departments.name"]
    changed = resolve_semantic_contract(
        accepted, original_question="Remove the department filter", resolved_question="Net revenue",
        changes=[RequestedChange(operation="REMOVE_FILTER", target="departments.name")],
    )
    assert changed.filters == []
    assert ("departments.name" in changed.required_columns) == still_grouped
    assert "departments.id" not in changed.required_columns
    assert "orders.amount" in changed.required_columns
    assert accepted.filters[0].value == "A"


def test_real_sqlite_filter_add_then_remove_replans_required_joins(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    registry, document = fixture(tmp_path)
    with sqlite3.connect(registry.resolve("shop")) as connection:
        connection.execute("INSERT INTO orders VALUES (4,2,100,'complete','2026-10-04',10)")
    monkeypatch.setenv("TEXT2SQL_KPI_FIRST_ENABLED", "true")
    model = SQLModel("SELECT SUM(o.amount)-SUM(o.refund_amount) AS net_revenue FROM orders o "
                     "JOIN customers c ON o.customer_id=c.id "
                     "JOIN departments d ON c.department_id=d.id "
                     "WHERE o.status='complete' AND d.name='A'")
    svc = service(tmp_path, registry, document, model)
    filtered = SemanticContract(original_question="Net revenue for department A",
                                filters=[SemanticFilter(operand="departments.name",
                                                        operator="=", value="A")])
    first = asyncio.run(svc.execute_question(
        question="Net revenue for department A", db_id="shop", provider="groq", model="scripted",
        max_attempts=1, semantic_contract=filtered,
    ))
    assert first.generated.accepted, first.generated.model_error
    assert first.generated.rows == [[135.0]]
    assert set(first.approved_tables) == {"orders", "customers", "departments"}
    changed = resolve_semantic_contract(
        first.semantic_contract, original_question="Remove department filter",
        resolved_question="Net revenue total", changes=[RequestedChange(
            operation="REMOVE_FILTER", target="departments.name")],
    )
    model.sql = ("SELECT SUM(amount)-SUM(refund_amount) AS net_revenue FROM orders "
                 "WHERE status='complete'")
    second = asyncio.run(svc.execute_question(
        question="Net revenue total", db_id="shop", provider="groq", model="scripted",
        max_attempts=1, semantic_contract=changed,
    ))
    assert second.generated.accepted, second.generated.model_error
    assert second.generated.rows == [[225.0]]
    assert second.approved_tables == ["orders"] and model.calls == 2
    assert first.semantic_contract.filters[0].value == "A"


def test_temporal_filter_pinning_and_missing_filter_column_fail_closed(tmp_path: Path) -> None:
    registry, document = fixture(tmp_path)
    schema = registry.inspect("shop")
    resolution = resolve_kpi_candidates("net_revenue", schema, document)
    contract, _ = resolve_metrics("net_revenue", schema, document, SemanticContract(
        temporal_boundaries=[SemanticFilter(operand="orders.ordered_on", operator=">=",
                                             value="2026-01-01")]))
    grounding = build_kpi_grounding(resolution, contract, document, "net_revenue", schema=schema)
    assert "orders.ordered_on" in grounding.mandatory_columns
    contract.filters = [SemanticFilter(operand="missing.secret", operator="=", value="x")]
    with pytest.raises(ValueError, match="KPI_FILTER_COLUMN_UNAVAILABLE"):
        build_kpi_grounding(resolution, contract, document, "net_revenue", schema=schema)


def test_explicit_physical_outputs_are_pinned_without_guessing_new_grouping(tmp_path: Path) -> None:
    registry, document = fixture(tmp_path)
    schema = registry.inspect("shop")
    resolution = resolve_kpi_candidates("net_revenue", schema, document)
    contract, _ = resolve_metrics("net_revenue", schema, document,
                                  SemanticContract(outputs=["orders.ordered_on", "net_revenue"]))
    grounding = build_kpi_grounding(resolution, contract, document, "Net revenue", schema=schema)
    assert "orders.ordered_on" in grounding.mandatory_columns
    assert "net_revenue" not in grounding.mandatory_columns
    assert grounding.requested_dimensions == []
    contract.outputs = ["missing.secret"]
    with pytest.raises(ValueError, match="KPI_OUTPUT_COLUMN_UNAVAILABLE"):
        build_kpi_grounding(resolution, contract, document, "Net revenue", schema=schema)


def test_combined_root_metrics_require_shared_dimension_support(tmp_path: Path) -> None:
    registry, document = fixture(tmp_path)
    schema = registry.inspect("shop")
    resolution = resolve_kpi_candidates("net_revenue and refunds", schema, document)
    contract, _ = resolve_metrics("net_revenue and refunds", schema, document,
                                  SemanticContract(grain=["departments.name"]),
                                  metric_ids=resolution.selected_ids)
    with pytest.raises(ValueError, match="KPI_DIMENSION_UNSUPPORTED"):
        build_kpi_grounding(resolution, contract, document, "by department", schema=schema)


def test_generic_followup_preserves_requested_roots_not_all_dependency_metrics(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    registry, document = fixture(tmp_path)
    monkeypatch.setenv("TEXT2SQL_KPI_FIRST_ENABLED", "true")
    model = SQLModel("SELECT SUM(amount)-SUM(refund_amount) AS net_revenue FROM orders "
                     "WHERE status='complete'")
    svc = service(tmp_path, registry, document, model)
    first = execute(svc, "Net revenue")
    assert first.generated.accepted
    changed = resolve_semantic_contract(
        first.semantic_contract, original_question="Group the same metric by department",
        resolved_question="Same metric by department", changes=[RequestedChange(
            operation="ADD_DIMENSION", target="departments.name", new_value="departments.name")],
    )
    model.sql = ("SELECT d.name, SUM(o.amount)-SUM(o.refund_amount) AS net_revenue "
                 "FROM orders o JOIN customers c ON o.customer_id=c.id "
                 "JOIN departments d ON c.department_id=d.id "
                 "WHERE o.status='complete' GROUP BY d.name")
    second = asyncio.run(svc.execute_question(
        question="Same metric by department", db_id="shop", provider="groq", model="scripted",
        max_attempts=1, semantic_contract=changed,
    ))
    assert second.generated.accepted, second.generated.model_error
    assert second.generated.rows == [["A", 135.0]]
    resolution = second.semantic_contract.kpi_grounding.resolution
    assert resolution.selected_ids == ["net_revenue"]
    assert resolution.status == "RESOLVED"
    assert resolution.candidates[0].method == "accepted_state"
    assert resolution.candidates[0].authoritative_identity
    assert model.calls == 2
