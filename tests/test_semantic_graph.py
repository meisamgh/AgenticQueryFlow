from __future__ import annotations

from pathlib import Path

import pytest
from test_semantic_catalog import APPROVAL, _document, _schema

from semantic_text2sql.glossary import GlossaryStore
from semantic_text2sql.graph_grounding import resolve_metrics
from semantic_text2sql.join_resolver import resolve_joins
from semantic_text2sql.models import (
    ColumnInfo,
    ForeignKeyInfo,
    SchemaInfo,
    SemanticContract,
    TableInfo,
)
from semantic_text2sql.ontology import LocalOntologyAdapter
from semantic_text2sql.recovery import _validated_claims, ontology_recovery_allowed
from semantic_text2sql.semantic_catalog import SemanticCatalogStore, schema_fingerprint
from semantic_text2sql.semantic_graph import SemanticGraphStore, compile_graph, graph_identity


def join_schema(edges: list[tuple[str, str]]) -> SchemaInfo:
    names = sorted({name for pair in edges for name in pair})
    return SchemaInfo(
        db_id="graph",
        tables=[
            TableInfo(
                name=name,
                create_sql="",
                columns=[
                    ColumnInfo(name="id", data_type="INTEGER", primary_key=True),
                    ColumnInfo(name="part", data_type="INTEGER"),
                ],
            )
            for name in names
        ],
        relationships=[
            ForeignKeyInfo(from_table=a, from_column="id", to_table=b, to_column="id")
            for a, b in edges
        ],
    )


def approved_document():
    document = _document()
    document.metrics[0] = document.metrics[0].model_copy(
        update={"status": "approved", "approval": APPROVAL}
    )
    return document


def test_snapshot_roundtrip_cache_drift_and_corruption(tmp_path: Path) -> None:
    schema = _schema()
    document = approved_document()
    snapshot = compile_graph(schema, document)
    store = SemanticGraphStore(tmp_path)
    path = store.save(snapshot)
    assert store.load("shop", identity=snapshot.identity) == snapshot
    network = store.network("shop", identity=snapshot.identity)
    assert network is store.network("shop", identity=snapshot.identity)
    assert network is not None and network.is_directed() and network.is_multigraph()
    assert SemanticGraphStore(tmp_path).load("shop", identity=snapshot.identity) == snapshot
    assert store.load("shop", identity="stale") is None
    assert store.load("other", identity=snapshot.identity) is None
    schema.tables[0].columns.pop()
    assert graph_identity(schema, document) != snapshot.identity
    import json

    payload = json.loads(path.read_text())
    payload["nodes"][0]["facts"]["grain"] = "corrupted but valid JSON"
    path.write_text(json.dumps(payload))
    assert store.load("shop", identity=snapshot.identity) is None
    path.write_text('{"bad":true}')
    assert store.load("shop", identity=snapshot.identity) is None


def test_composite_fk_direction_and_no_profile_values() -> None:
    schema = join_schema([("child", "parent")])
    schema.relationships = [
        ForeignKeyInfo(
            from_table="child",
            from_column=col,
            to_table="parent",
            to_column=col,
            constraint_id="fk1",
            ordinal=i,
        )
        for i, col in enumerate(["id", "part"])
    ]
    graph = compile_graph(schema)
    edges = [edge for edge in graph.edges if edge.kind == "REFERENCES"]
    assert len(edges) == 1 and edges[0].source == "table:child"
    assert edges[0].facts["child_columns"] == ["id", "part"]
    result = resolve_joins(["parent", "child"], schema, snapshot=graph)
    assert result.status == "CONNECTED" and result.relationships[0].facts["parent_columns"] == [
        "id",
        "part",
    ]


@pytest.mark.parametrize(
    "edges,seeds,status,bridges",
    [
        ([("a", "b"), ("b", "c")], ["a", "c"], "CONNECTED", ["b"]),
        ([("a", "b"), ("b", "c"), ("a", "d"), ("d", "c")], ["a", "c"], "AMBIGUOUS", []),
        ([("a", "b"), ("c", "d")], ["a", "d"], "DISCONNECTED", []),
    ],
)
def test_paths(edges, seeds, status, bridges) -> None:
    schema = join_schema(edges)
    result = resolve_joins(seeds, schema, snapshot=compile_graph(schema))
    assert result.status == status and result.bridges == bridges


def test_permissions_stale_snapshots_cycles_and_limits() -> None:
    schema = join_schema([("a", "b"), ("b", "c"), ("c", "a")])
    graph = compile_graph(schema)
    assert resolve_joins(["a", "c"], schema, allowed_tables={"a"}).status == "DISCONNECTED"
    assert resolve_joins(["a", "c"], schema, max_candidates=0).status == "BUDGET_EXHAUSTED"
    assert resolve_joins(["a", "c"], schema, timeout_seconds=0).status == "BUDGET_EXHAUSTED"
    graph.db_id = "different"
    assert resolve_joins(["a", "c"], schema, snapshot=graph).status == "DISCONNECTED"


def test_metric_approval_drift_filters_dependencies_and_cycles() -> None:
    schema = _schema()
    document = approved_document()
    metric = document.metrics[0]
    contract, facts = resolve_metrics("Order total", schema, document, SemanticContract())
    assert contract.approved_metrics[0].formula == "SUM(amount)"
    assert contract.required_columns == ["orders.amount"]
    assert facts[0]["unit"] == "eur" and facts[0]["version"] == 1
    assert resolve_metrics("Order total", schema, _document(), SemanticContract())[1] == []
    metric.dependencies = [metric.id]
    with pytest.raises(ValueError, match="unsupported"):
        resolve_metrics("Order total", schema, document, SemanticContract())
    metric.dependencies = []
    document.schema_fingerprint = "stale"
    with pytest.raises(ValueError, match="no longer"):
        resolve_metrics("Order total", schema, document, SemanticContract())
    with pytest.raises(ValueError, match="no longer"):
        resolve_metrics("Order total", schema, document, contract)


def test_ontology_scoping_and_typed_evidence(tmp_path: Path) -> None:
    schema, document = _schema(), approved_document()
    catalog_root = tmp_path / "catalog"
    catalog_root.mkdir()
    (catalog_root / "shop.json").write_text(document.model_dump_json())
    graphs = SemanticGraphStore(tmp_path / "graph")
    graphs.save(compile_graph(schema, document))
    ontology = LocalOntologyAdapter(
        schema, None, SemanticCatalogStore(catalog_root), graphs, GlossaryStore()
    )
    result = ontology.lookup("Order total", allowed_tables={"orders"})
    assert len(result["metrics"]) == 1
    assert ontology.lookup("Order total", allowed_tables=set())["metrics"] == []
    observations = [{"evidence_id": "observation-1", "tool": "query_ontology", "result": result}]
    claim = {
        "claim_type": "METRIC_DEFINITION",
        "metric_id": "order_total",
        "table": "orders",
        "value": "SUM(amount)",
        "statement": "Approved formula",
        "evidence_ids": ["observation-1"],
    }
    assert _validated_claims([claim], observations, require_tool_evidence=True)
    assert not _validated_claims(
        [{**claim, "value": "AVG(amount)"}], observations, require_tool_evidence=True
    )
    assert not _validated_claims(
        [{**claim, "claim_type": "VALUE_ABSENT"}], observations, require_tool_evidence=True
    )


def test_ontology_only_for_semantic_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEXT2SQL_ONTOLOGY_RECOVERY_ENABLED", "true")
    assert ontology_recovery_allowed("CORRECTNESS_REVIEW")
    for reason in ["SQL_PARSE_ERROR", "PROVIDER_TIMEOUT", "READ_ONLY_SAFETY", "ZERO_RESULT"]:
        assert not ontology_recovery_allowed(reason)


def test_graph_cli_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    from semantic_text2sql.graph_cli import main

    schema_path = tmp_path / "schema.json"
    schema_path.write_text(_schema().model_dump_json())
    args = ["graph_cli", "build", "--schema", str(schema_path), "--root", str(tmp_path / "graphs")]
    monkeypatch.setattr(sys, "argv", args)
    main()
    assert ".json" in capsys.readouterr().out
    args[1] = "validate"
    main()
    assert "Compatible" in capsys.readouterr().out


@pytest.mark.parametrize(
    "enabled,snapshot_available,context_mode,metric_only",
    [
        (False, False, "retrieval", False),
        (False, False, "retrieval", True),
        (True, False, "retrieval", False),
        (True, True, "retrieval", False),
        (True, True, "model1", False),
    ],
)
def test_sql_service_approved_metric_grounding(
    tmp_path, monkeypatch, enabled, snapshot_available, context_mode, metric_only
):
    import asyncio

    from test_semantic_catalog import _registry

    from semantic_text2sql.agent import TextToSQLAgent
    from semantic_text2sql.historical import HistoricalQueryStore
    from semantic_text2sql.hybrid_retrieval import HybridSchemaRetriever
    from semantic_text2sql.profiling import ProfileStore
    from semantic_text2sql.service import TextToSQLService

    registry = _registry(tmp_path)
    catalog_root = tmp_path / "catalog"
    catalog_root.mkdir()
    document = approved_document()
    (catalog_root / "shop.json").write_text(document.model_dump_json())
    graphs = SemanticGraphStore(tmp_path / "graph")
    if snapshot_available:
        graphs.save(compile_graph(registry.inspect("shop"), document))

    class Encoder:
        def encode(self, texts):
            return [[1.0, 0.0] for _ in texts]

    class Model:
        calls = 0
        context = ""

        async def generate(self, **kwargs):
            self.calls += 1
            self.context = kwargs["profile_context"]
            return "SELECT SUM(amount) AS order_total FROM orders", 1

    model = Model()
    service = TextToSQLService(
        database=registry,
        profiles=ProfileStore(tmp_path / "profiles"),
        glossaries=GlossaryStore(),
        history=HistoricalQueryStore(),
        retriever=HybridSchemaRetriever(Encoder()),
        agent=TextToSQLAgent(registry, model),
        context_completers={"groq": object()},
        semantic_catalogs=SemanticCatalogStore(catalog_root),
        graph_store=graphs,
    )
    monkeypatch.setenv("TEXT2SQL_GRAPH_ENABLED", str(enabled).lower())
    monkeypatch.setenv("TEXT2SQL_KPI_GROUNDING_ENABLED", str(metric_only).lower())
    result = asyncio.run(
        service.execute_question(
            question="Order total",
            db_id="shop",
            provider="groq",
            model="scripted",
            max_attempts=1,
            context_mode=context_mode,
        )
    )
    assert result.generated.accepted and result.generated.rows == [[170.0]]
    assert model.calls == 1
    assert bool(result.semantic_contract.approved_metrics) == (enabled or metric_only)
    assert ("human_approved_semantic_catalog" in model.context) == (enabled or metric_only)
    assert result.planner_usage.total_tokens in (None, 0)
    assert not result.generated.telemetry.planner_call_used
    assert result.generated.telemetry.model1_latency_ms == 0
    assert result.generated.telemetry.context_grounding_latency_ms is not None
    assert not any(item.component == "context_planner"
                   for item in result.generated.telemetry.call_ledger)
    assert {"order_id", "customer_id"} <= set(result.generated.context_request.columns["orders"])
    if enabled or metric_only:
        document.schema_fingerprint = "stale"
        (catalog_root / "shop.json").write_text(document.model_dump_json())
        rejected = asyncio.run(
            service.execute_question(
                question="Order total again",
                db_id="shop",
                provider="groq",
                model="scripted",
                max_attempts=1,
                semantic_contract=result.semantic_contract,
            )
        )
        assert not rejected.generated.accepted
        assert rejected.generated.termination_reason == "context_error"
        assert model.calls == 1


def test_approved_join_overrides_forbidden_and_inferred_evidence() -> None:
    from semantic_text2sql.semantic_catalog import (
        ColumnCatalogEntry,
        JoinCatalogEntry,
        TableCatalogEntry,
    )

    schema = join_schema([("a", "b")])
    document = approved_document()
    document.source_db_id = "graph"
    document.entities = [
        TableCatalogEntry(
            id=name,
            relation=name,
            description=name,
            columns={"id": ColumnCatalogEntry(role="identifier")},
        )
        for name in ["a", "b"]
    ]
    document.metrics = []
    join = JoinCatalogEntry(
        child_table="a",
        child_columns=["id"],
        parent_table="b",
        parent_columns=["id"],
        source="manual",
        cardinality="many_to_many",
        status="approved",
        approval=APPROVAL,
    )
    document.relationships = [join]
    graph = compile_graph(schema, document)
    result = resolve_joins(["a", "b"], schema, snapshot=graph)
    assert result.relationships[0].provenance == "approved_catalog"
    assert result.relationships[0].facts["cardinality"] == "many_to_many"
    join.forbidden = True
    schema.relationships[0] = ForeignKeyInfo(
        from_table="b", from_column="id", to_table="a", to_column="id"
    )
    assert (
        resolve_joins(["a", "b"], schema, snapshot=compile_graph(schema, document)).status
        == "DISCONNECTED"
    )
    schema.relationships = []
    join.forbidden = False
    join.status, join.approval = "inferred", None
    assert (
        resolve_joins(["a", "b"], schema, snapshot=compile_graph(schema, document)).status
        == "DISCONNECTED"
    )


def test_dependencies_filters_conflicts_and_ambiguity() -> None:
    from semantic_text2sql.models import SemanticFilter

    schema, document = _schema(), approved_document()
    base = document.metrics[0]
    base.mandatory_filters = [
        SemanticFilter(operand="orders.status", operator="=", value="complete")
    ]
    derived = base.model_copy(
        update={"id": "derived", "label": "Derived total", "dependencies": [base.id]}
    )
    document.metrics.append(derived)
    contract, facts = resolve_metrics("Derived total", schema, document, SemanticContract())
    assert {metric.id for metric in contract.approved_metrics} == {"derived", "order_total"}
    assert contract.mandatory_metric_filters[0].value == "complete"
    assert facts[1]["dependencies"] == [base.id]
    with pytest.raises(ValueError, match="conflicts"):
        resolve_metrics(
            "Order total",
            schema,
            document,
            SemanticContract(
                filters=[SemanticFilter(operand="orders.status", operator="=", value="pending")]
            ),
        )
    derived.label = base.label
    derived.formula = "AVG(amount)"
    with pytest.raises(ValueError, match="conflicting"):
        resolve_metrics("Order total", schema, document, SemanticContract())


def test_ontology_recovery_reuses_existing_agent_and_budget(registry, monkeypatch) -> None:
    import asyncio
    import json

    from semantic_text2sql.models import TokenUsage
    from semantic_text2sql.recovery import RecoveryCoordinator, RecoveryTools
    from semantic_text2sql.runtime import RequestBudget

    monkeypatch.setenv("TEXT2SQL_ONTOLOGY_RECOVERY_ENABLED", "true")
    tools = RecoveryTools(registry, "shop", registry.inspect("shop"), None)
    evidence = {
        "available": True,
        "evidence_type": "APPROVED_ONTOLOGY",
        "db_id": "shop",
        "schema_fingerprint": schema_fingerprint(tools.schema),
        "catalog_version": 1,
        "authorized_tables": ["orders"],
        "metrics": [
            {
                "id": "order_total",
                "formula": "SUM(amount)",
                "source_relation": "orders",
                "status": "approved",
                "provenance": "human_approved_semantic_catalog",
                "catalog_version": 1,
            }
        ],
    }
    monkeypatch.setattr(tools, "query_ontology", lambda *args, **kwargs: evidence)

    class Model:
        calls = 0

        async def complete_detailed(self, *args):
            self.calls += 1
            if self.calls == 1:
                payload = {
                    "action": "CALL_TOOL",
                    "tool": "query_ontology",
                    "arguments": {"query": "Order total"},
                    "purpose": "Check definition",
                }
            else:
                payload = {
                    "action": "INFORM",
                    "diagnosis": "The approved total sums amounts.",
                    "confidence": 0.9,
                    "claims": [
                        {
                            "claim_type": "METRIC_DEFINITION",
                            "statement": "Approved formula",
                            "metric_id": "order_total",
                            "table": "orders",
                            "value": "SUM(amount)",
                            "evidence_ids": ["observation-1"],
                        }
                    ],
                }
            return json.dumps(payload), TokenUsage(input_tokens=20, output_tokens=10)

    model = Model()
    budget = RequestBudget(timeout_seconds=2, max_model_calls=2, max_database_calls=0)
    trace = asyncio.run(
        RecoveryCoordinator(tools).ainvestigate(
            question="Check order total",
            failed_sql="SELECT AVG(amount) FROM orders",
            failure_code="METRIC_MISMATCH",
            failure_message="Wrong aggregation",
            allowed_tables=["orders"],
            completer=model,
            model="scripted",
            budget=budget,
        )
    )
    assert model.calls == budget.model_calls == 2
    assert budget.database_calls == trace.usage.database_probe_count == 0
    assert trace.agent_action == "INFORM"
    assert trace.claims[0].metric_id == "order_total"


def test_profile_sample_values_never_enter_snapshot() -> None:
    from semantic_text2sql.models import ColumnProfile, DatabaseProfile, ValueFrequency

    schema = _schema()
    profile = DatabaseProfile(
        db_id="shop",
        dialect="sqlite",
        profiled_at="v1",
        columns=[
            ColumnProfile(
                table="orders",
                column="status",
                database_type="TEXT",
                semantic_type="text",
                row_count=1,
                null_count=0,
                null_ratio=0,
                examples=["PRIVATE_SAMPLE"],
                top_values=[ValueFrequency(value="PRIVATE_SAMPLE", count=1)],
            )
        ],
    )
    snapshot = compile_graph(schema, profile=profile)
    assert "PRIVATE_SAMPLE" not in snapshot.model_dump_json()
    profile.profiled_at = "v2"
    assert graph_identity(schema, profile=profile) != snapshot.identity


def test_disabled_retrieval_retains_legacy_choice_and_enabled_reports_ambiguity(
    monkeypatch,
) -> None:
    from semantic_text2sql.hybrid_retrieval import _expand_bridges

    schema = join_schema([("a", "b"), ("b", "c"), ("a", "d"), ("d", "c")])
    monkeypatch.setenv("TEXT2SQL_GRAPH_ENABLED", "false")
    assert _expand_bridges(["a", "c"], schema) == ["a", "c", "b"]
    monkeypatch.setenv("TEXT2SQL_GRAPH_ENABLED", "true")
    assert _expand_bridges(["a", "c"], schema) == ["a", "c"]


def test_catalog_context_preserves_composite_mapping_and_cannot_be_forged() -> None:
    from pydantic import ValidationError

    from semantic_text2sql.context import build_context_plan
    from semantic_text2sql.models import ContextRelationship, GenerateRequest

    schema = join_schema([("a", "b")])
    schema.relationships = []
    relationship = ContextRelationship(
        left_table="a",
        right_table="b",
        left_column="id",
        right_column="id",
        left_columns=["id", "part"],
        right_columns=["id", "part"],
        state="APPROVED_CATALOG",
        join_cardinality="MANY_TO_ONE",
        fanout_risk=False,
        recommended_strategy="JOIN",
    )
    plan = build_context_plan(
        schema,
        None,
        SemanticContract(),
        "Join both tables",
        None,
        catalog_relationships=[relationship],
    )
    assert plan.relationships[0].state == "APPROVED_CATALOG"
    assert plan.relationships[0].left_columns == ["id", "part"]
    assert not any(item.startswith("JOIN_PATH:") for item in plan.missing_requirements)
    with pytest.raises(ValidationError):
        GenerateRequest.model_validate(
            {
                "db_id": "graph",
                "question": "hello",
                "_catalog_relationships": [relationship.model_dump()],
            }
        )


def test_wrong_simple_metric_aggregation_is_not_accepted_as_correct() -> None:
    from semantic_text2sql.models import ApprovedMetricDefinition
    from semantic_text2sql.validator import validate_sql_semantics

    contract = SemanticContract(
        approved_metrics=[
            ApprovedMetricDefinition(
                id="total",
                formula="SUM(amount)",
                source_relation="orders",
                required_fields=["amount"],
            )
        ]
    )
    validation = validate_sql_semantics("SELECT AVG(amount) FROM orders", contract)
    assert any(item.status == "FAIL" for item in validation)


def test_ontology_definitions_do_not_authorize_unproven_repairs() -> None:
    from semantic_text2sql.models import RecoveryClaim
    from semantic_text2sql.recovery import ontology_repair_supported

    claims = [
        RecoveryClaim(
            claim_type="METRIC_DEFINITION",
            metric_id="order_total",
            table="orders",
            value="SUM(amount)",
            statement="Approved total",
            evidence_ids=["observation-1"],
        )
    ]
    assert ontology_repair_supported("SELECT AVG(amount) FROM orders", claims, _schema())
    assert not ontology_repair_supported("SELECT SUM(amount) FROM orders", claims, _schema())
    assert not ontology_repair_supported("SELECT ???", claims, _schema())


@pytest.mark.parametrize("persist", [False, True])
def test_application_startup_builds_metadata_graph_without_queries(
    tmp_path, monkeypatch, persist,
) -> None:
    from fastapi.testclient import TestClient
    from test_semantic_catalog import _registry

    from semantic_text2sql.agent import TextToSQLAgent
    from semantic_text2sql.api import create_app
    from semantic_text2sql.database import DatabaseRegistry

    registry = _registry(tmp_path)
    monkeypatch.setenv("TEXT2SQL_GRAPH_ENABLED", "true")
    monkeypatch.setenv("TEXT2SQL_GRAPH_PERSISTENCE_ENABLED", str(persist).lower())
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(tmp_path))
    monkeypatch.setenv("TEXT2SQL_GRAPH_ROOT", str(tmp_path / "graphs"))
    monkeypatch.setenv("TEXT2SQL_PROFILE_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setenv("TEXT2SQL_GLOSSARY_ROOT", str(tmp_path / "glossaries"))
    monkeypatch.setenv("TEXT2SQL_SEMANTIC_CATALOG_ROOT", str(tmp_path / "catalogs"))
    monkeypatch.setenv("TEXT2SQL_EMBEDDING_PRELOAD", "false")
    monkeypatch.setenv("TEXT2SQL_POSTGRES_DATABASES", "{}")
    monkeypatch.setenv("TEXT2SQL_EXTERNAL_CONNECTIONS", "[]")

    def forbidden(*args, **kwargs):
        raise AssertionError("Graph construction must not execute SQL against business rows")

    monkeypatch.setattr(DatabaseRegistry, "execute", forbidden)

    class Model:
        async def generate(self, **kwargs):
            raise AssertionError("No LLM allowed for graph construction")

    captured = {}
    remember = SemanticGraphStore.remember

    def cache(store, snapshot):
        captured[snapshot.db_id] = store
        remember(store, snapshot)

    monkeypatch.setattr(SemanticGraphStore, "remember", cache)
    with TestClient(create_app(agent=TextToSQLAgent(registry, Model()))) as client:
        assert client.get("/").status_code == 200
    identity = graph_identity(registry.inspect("shop"))
    assert captured["shop"].load("shop", identity=identity) is not None
    if persist:
        assert SemanticGraphStore(tmp_path / "graphs").load("shop", identity=identity) is not None
    else:
        assert not (tmp_path / "graphs").exists()


def test_compact_context_preserves_formulas_and_rejects_oversize_metadata() -> None:
    from semantic_text2sql.graph_grounding import compact_grounding

    _, metrics = resolve_metrics("Order total", _schema(), approved_document(), SemanticContract())
    context = compact_grounding(metrics, None)
    assert "SUM(amount)" in context and "human_approved_semantic_catalog" in context
    assert "approved_by" not in context
    with pytest.raises(ValueError, match="bounded context"):
        compact_grounding(metrics, None, max_chars=10)


def test_approved_metric_preserves_exact_schema_qualification() -> None:
    schema, document = _schema(), approved_document()
    schema.tables[0].name = "analytics.orders"
    document.entities[0].relation = "analytics.orders"
    document.metrics[0].source_relation = "analytics.orders"
    document.metrics[0].formula = "SUM(analytics.orders.amount)"
    contract, _ = resolve_metrics("Order total", schema, document, SemanticContract())
    assert contract.required_columns == ["analytics.orders.amount"]
    assert contract.approved_metrics[0].source_relation == "analytics.orders"
    document.metrics[0].formula = "SUM(forbidden.orders.amount)"
    with pytest.raises(ValueError, match="unsupported"):
        resolve_metrics("Order total", schema, document, SemanticContract())


def test_disabled_graph_keeps_legacy_long_bridge_paths(monkeypatch) -> None:
    from semantic_text2sql.hybrid_retrieval import _expand_bridges

    schema = join_schema([(f"t{i}", f"t{i + 1}") for i in range(9)])
    monkeypatch.setenv("TEXT2SQL_GRAPH_ENABLED", "false")
    assert _expand_bridges(["t0", "t9"], schema) == ["t0", "t9", *[f"t{i}" for i in range(1, 9)]]
    monkeypatch.setenv("TEXT2SQL_GRAPH_ENABLED", "true")
    assert _expand_bridges(["t0", "t9"], schema) == ["t0", "t9"]
