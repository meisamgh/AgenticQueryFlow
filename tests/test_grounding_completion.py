"""Grounding changes retain source authorization, bounded work and approved meaning."""
from pathlib import Path

import pytest
from test_semantic_catalog import _schema
from test_semantic_graph import approved_document, join_schema

from semantic_text2sql.conversation import resolve_semantic_contract
from semantic_text2sql.graph_grounding import resolve_metrics
from semantic_text2sql.join_resolver import resolve_joins
from semantic_text2sql.models import ApprovedMetricDefinition, RequestedChange, SemanticContract
from semantic_text2sql.semantic_graph import SemanticGraphStore, compile_graph


def test_dense_seven_table_schema_does_not_enumerate_all_cycles() -> None:
    names = [f"t{i}" for i in range(7)]
    schema = join_schema([(a, b) for index, a in enumerate(names) for b in names[index + 1:]])
    resolution = resolve_joins(names[:5], schema, snapshot=compile_graph(schema))
    assert resolution.status == "CONNECTED"
    assert resolution.examined_states <= 64
    assert resolution.relationships


def test_ambiguity_retains_typed_candidate_provenance() -> None:
    schema = join_schema([("a", "b"), ("b", "d"), ("a", "c"), ("c", "d")])
    result = resolve_joins(["a", "d"], schema, snapshot=compile_graph(schema))
    assert result.status == "AMBIGUOUS" and not result.relationships
    assert len(result.warnings[0].candidates) == 2
    assert all(item.provenance == ["declared_foreign_key"] * 2
               for item in result.warnings[0].candidates)
    assert resolve_joins(["a", "d"], schema, reject_ambiguous=False).status == "CONNECTED"


def test_memory_graph_does_not_read_or_create_persistent_files(tmp_path: Path) -> None:
    store = SemanticGraphStore(tmp_path / "absent", persistence_enabled=False)
    snapshot = compile_graph(_schema())
    store.remember(snapshot)
    assert store.load(snapshot.db_id, identity=snapshot.identity) == snapshot
    assert not store.root.exists()
    assert store.load(snapshot.db_id, identity="stale") is None
    assert SemanticGraphStore(store.root, persistence_enabled=False).load(
        snapshot.db_id, identity=snapshot.identity) is None
    # Caller mutations cannot change cached approved facts.
    copy = store.load(snapshot.db_id, identity=snapshot.identity)
    assert copy is not None
    copy.nodes.clear()
    assert store.load(snapshot.db_id, identity=snapshot.identity) == snapshot


@pytest.mark.parametrize("field,new", [("unit", "usd"), ("version", 2),
                                        ("valid_time_grains", ["weekly"])])
def test_followup_rejects_changed_approved_metric_metadata(field, new):  # type: ignore[no-untyped-def]
    schema, document = _schema(), approved_document()
    contract, _ = resolve_metrics("Order total", schema, document, SemanticContract())
    setattr(document.metrics[0], field, new)
    with pytest.raises(ValueError, match="changed"):
        resolve_metrics("Same total", schema, document, contract)


def test_approved_aggregation_cannot_be_changed_without_metric_review() -> None:
    contract = SemanticContract(approved_metrics=[ApprovedMetricDefinition(
        id="approved_total", source_relation="sales", formula="SUM(price)")])
    with pytest.raises(ValueError, match="approved"):
        resolve_semantic_contract(contract, original_question="Average instead",
                                  resolved_question="Average instead", changes=[RequestedChange(
                                      operation="CHANGE_AGGREGATION", target="aggregation",
                                      new_value="average")])
    assert contract.approved_metrics[0].formula == "SUM(price)"


def test_period_change_preserves_explicit_day_and_unaffected_metric() -> None:
    from semantic_text2sql.models import SemanticFilter

    old = SemanticContract(active_period="2013", measures=["spend"], filters=[
        SemanticFilter(operand="orders.Date", operator="=", value="2013-08-25"),
        SemanticFilter(operand="orders.Currency", operator="=", value="EUR"),
    ])
    new = resolve_semantic_contract(old, original_question="Now 2014", resolved_question="2014",
                                    changes=[RequestedChange(operation="CHANGE_PERIOD",
                                                             target="year", new_value="2014")])
    assert [item.value for item in new.filters] == ["2014-08-25", "EUR"]
    assert new.measures == old.measures and old.filters[0].value == "2013-08-25"


def test_comparison_filter_can_remove_one_member_without_losing_scope() -> None:
    from semantic_text2sql.models import SemanticFilter

    old = SemanticContract(active_period="2024", filters=[
        SemanticFilter(operand="country", operator="IN", value=["Germany", "France"]),
    ])
    new = resolve_semantic_contract(old, original_question="Remove France",
                                    resolved_question="Remove France", changes=[RequestedChange(
                                        operation="REMOVE_FILTER", target="country",
                                        previous_value="France")])
    assert new.filters[0].value == "Germany" and new.filters[0].operator == "="
    assert new.active_period == "2024"


def test_ontology_claim_rejects_stale_version_and_wrong_datasource() -> None:
    from semantic_text2sql.recovery import _validated_claims

    evidence = {"available": True, "evidence_type": "APPROVED_ONTOLOGY", "db_id": "shop",
                "schema_fingerprint": "verified", "catalog_version": 2,
                "authorized_tables": ["orders"], "metrics": [{
                    "id": "total", "source_relation": "orders", "formula": "SUM(amount)",
                    "status": "approved", "provenance": "human_approved_semantic_catalog",
                    "catalog_version": 2}]}
    claim = {"claim_type": "METRIC_DEFINITION", "metric_id": "total", "table": "orders",
             "value": "SUM(amount)", "statement": "Approved definition",
             "evidence_ids": ["observation-1"]}

    def valid(payload):  # type: ignore[no-untyped-def]
        return _validated_claims([claim], [{"evidence_id": "observation-1",
                                           "tool": "query_ontology", "result": payload}],
                                 require_tool_evidence=True, expected_db_id="shop",
                                 expected_schema_fingerprint="verified")

    assert valid(evidence)
    for changes in ({"db_id": "other"}, {"schema_fingerprint": "stale"},
                    {"catalog_version": 1}, {"authorized_tables": []}):
        assert not valid({**evidence, **changes})


def test_cached_network_is_source_scoped_and_cannot_be_changed_via_result(tmp_path: Path) -> None:
    schema = join_schema([("a", "b")])
    snapshot = compile_graph(schema)
    store = SemanticGraphStore(tmp_path, persistence_enabled=False)
    store.remember(snapshot)
    network = store.network(schema.db_id, identity=snapshot.identity)
    result = resolve_joins(["a", "b"], schema, snapshot=snapshot, compiled_network=network)
    assert result.status == "CONNECTED"
    result.relationships[0].facts["forbidden"] = True
    again = resolve_joins(["a", "b"], schema, snapshot=snapshot, compiled_network=network)
    assert not again.relationships[0].facts.get("forbidden")
    incompatible = compile_graph(_schema()).network()
    assert resolve_joins(["a", "b"], schema, snapshot=snapshot,
                         compiled_network=incompatible).status == "DISCONNECTED"


def test_metric_discovery_uses_same_current_catalog_boundary(tmp_path: Path) -> None:
    from test_conversational_bridge import SmallEncoder
    from test_semantic_catalog import _registry

    from semantic_text2sql.conversational_bridge import (
        ConversationalAnalyticsBridge,
        MetadataToolInput,
    )
    from semantic_text2sql.glossary import GlossaryStore
    from semantic_text2sql.hybrid_retrieval import HybridSchemaRetriever
    from semantic_text2sql.profiling import ProfileStore
    from semantic_text2sql.semantic_catalog import SemanticCatalogStore

    registry = _registry(tmp_path)
    root = tmp_path / "catalog"
    root.mkdir()
    document = approved_document()
    path = root / "shop.json"
    path.write_text(document.model_dump_json())

    async def forbidden(*args):  # type: ignore[no-untyped-def]
        raise AssertionError("No SQL generation or execution during definition lookup")

    bridge = ConversationalAnalyticsBridge(
        database=registry, postgres=None, profiles=ProfileStore(tmp_path / "profiles"),
        glossaries=GlossaryStore(tmp_path / "glossaries"),
        retriever=HybridSchemaRetriever(SmallEncoder()), sql_handler=forbidden,
        semantic_catalogs=SemanticCatalogStore(root))
    request = MetadataToolInput(db_id="shop", question="What metrics are available?")
    assert any("SUM(amount)" in fact for fact in bridge.get_business_context(request).facts)
    document.schema_fingerprint = "stale"
    path.write_text(document.model_dump_json())
    assert not bridge.get_business_context(request).found
