from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from semantic_text2sql.api import create_app
from semantic_text2sql.database import DatabaseRegistry
from semantic_text2sql.models import ColumnInfo, SchemaInfo, TableInfo
from semantic_text2sql.semantic_catalog import (
    ApprovalRecord,
    CatalogDocument,
    ColumnCatalogEntry,
    GrainFactEntry,
    MetricCatalogEntry,
    QualityContractEntry,
    SemanticCatalogStore,
    TableCatalogEntry,
    TimeSemanticsEntry,
    schema_fingerprint,
)
from semantic_text2sql.semantic_catalog_validation import (
    sqlite_runner,
    verify_catalog,
)

APPROVAL = ApprovalRecord(
    approved_by="meisam",
    approved_at=datetime(2026, 10, 14, 12, 0, tzinfo=UTC),
    approval_source="human_review",
    evidence=["order_id declared as primary key in the shop fixture"],
)

_SHOP_SCHEMA = """
CREATE TABLE orders (
  order_id INTEGER PRIMARY KEY,
  customer_id INTEGER NOT NULL,
  amount REAL NOT NULL,
  status TEXT NOT NULL,
  ordered_on TEXT NOT NULL
);
INSERT INTO orders VALUES
  (1, 1, 100.0, 'complete', '2026-10-01'),
  (2, 1, 50.0, 'complete', '2026-10-02'),
  (3, 2, 20.0, 'pending', '2026-10-03');
"""


def _registry(
    tmp_path: Path,
    db_id: str = "shop",
    extra_sql: list[str] | None = None,
) -> DatabaseRegistry:
    directory = tmp_path / db_id
    directory.mkdir()
    with sqlite3.connect(directory / f"{db_id}.sqlite") as connection:
        connection.executescript(_SHOP_SCHEMA)
        if extra_sql:
            connection.executescript(";\n".join(extra_sql))
    return DatabaseRegistry(tmp_path)


def _grain(status: str = "approved") -> GrainFactEntry:
    return GrainFactEntry(
        keys=["order_id"],
        meaning="one row per order",
        entity_role="declared_primary_key",
        status=status,
        approval=APPROVAL if status == "approved" else None,
    )


def _document(grain_status: str = "approved") -> CatalogDocument:
    return CatalogDocument(
        catalog_version=1,
        id="shop",
        source_db_id="shop",
        owner="meisam",
        metadata_status="reviewed",
        entities=[
            TableCatalogEntry(
                id="orders_fact",
                relation="orders",
                description="Customer orders; one row per order.",
                columns={
                    "order_id": ColumnCatalogEntry(role="identifier"),
                    "customer_id": ColumnCatalogEntry(role="dimension"),
                    "amount": ColumnCatalogEntry(role="measure", unit="eur"),
                    "status": ColumnCatalogEntry(role="dimension"),
                    "ordered_on": ColumnCatalogEntry(role="time", semantic_type="trading_date"),
                },
                grain=_grain(grain_status),
                time=TimeSemanticsEntry(
                    event_time="ordered_on", timezone=None, update_mode="append"
                ),
                quality=QualityContractEntry(
                    required=["order_id", "status"], unique=[["order_id"]], positive=["amount"]
                ),
                status="reviewed",
            )
        ],
        metrics=[
            MetricCatalogEntry(
                id="order_total",
                label="Order total",
                description="Sum of order amounts.",
                source_relation="orders",
                formula="SUM(amount)",
                required_fields=["amount"],
                valid_time_grains=["daily"],
                unit="eur",
                format="number",
                aggregation="additive",
                status="inferred",
            )
        ],
    )


def _schema() -> SchemaInfo:
    return SchemaInfo(
        db_id="shop",
        dialect="sqlite",
        tables=[
            TableInfo(
                name="orders",
                create_sql="CREATE TABLE orders (...)",
                columns=[
                    ColumnInfo(name="order_id", data_type="INTEGER", primary_key=True),
                    ColumnInfo(name="customer_id", data_type="INTEGER"),
                    ColumnInfo(name="amount", data_type="REAL"),
                    ColumnInfo(name="status", data_type="TEXT"),
                    ColumnInfo(name="ordered_on", data_type="TEXT"),
                ],
            )
        ],
        relationships=[],
    )


def test_approval_rules_can_never_be_invented() -> None:
    with pytest.raises(ValueError):
        GrainFactEntry(
            keys=["a"],
            meaning="m",
            entity_role="asserted",
            status="approved",
            approval=None,
        )
    with pytest.raises(ValueError):
        MetricCatalogEntry(
            id="wrong",
            label="Wrong",
            description="Approval attached to an unapproved fact.",
            source_relation="orders",
            formula="SUM(amount)",
            required_fields=["amount"],
            valid_time_grains=["daily"],
            unit="eur",
            format="number",
            status="reviewed",
            approval=APPROVAL,
        )


def test_machine_authored_facts_stay_inferred_even_when_probes_pass(tmp_path: Path) -> None:
    document = _document()
    report = verify_catalog(
        document,
        _schema(),
        sqlite_runner(_registry(tmp_path), "shop"),
        "sqlite",
    )
    metric_claim = next(claim for claim in report.claims if claim.subject == "metric:order_total")
    assert metric_claim.status == "inferred"


def test_human_approved_grain_holds_under_probes(tmp_path: Path) -> None:
    document = _document()
    report = verify_catalog(
        document,
        _schema(),
        sqlite_runner(_registry(tmp_path), "shop"),
        "sqlite",
    )
    grain_claim = next(claim for claim in report.claims if claim.subject == "orders_fact.grain")
    assert grain_claim.status == "approved"
    entity_claim = next(claim for claim in report.claims if claim.subject_kind == "entity")
    assert entity_claim.status == "reviewed"


def test_duplicate_grain_rows_conflict_with_the_approval(tmp_path: Path) -> None:
    document = _document()
    conflicting_grain = document.entities[0].grain.model_copy(
        update={"keys": ["customer_id"], "meaning": "one row per customer",
                "entity_role": "asserted"}
    )
    drifting_entity = document.entities[0].model_copy(update={"grain": conflicting_grain})
    drifted = document.model_copy(update={"entities": [drifting_entity]})
    report = verify_catalog(
        drifted,
        _schema(),
        sqlite_runner(_registry(tmp_path), "shop"),
        "sqlite",
    )
    grain_claim = next(claim for claim in report.claims if claim.subject == "orders_fact.grain")
    assert grain_claim.status == "conflicting"
    assert report.conflicts, "duplicate grain rows are an explicit conflict"


def test_missing_relation_downgrades_to_stale(tmp_path: Path) -> None:
    drifting_document = _document().model_copy(deep=True)
    drifted_entity = drifting_document.entities[0].model_copy(update={"relation": "orders_missing"})
    drifted_metric = drifting_document.metrics[0].model_copy(
        update={"source_relation": "orders_missing"}
    )
    drifting_document = drifting_document.model_copy(
        update={"entities": [drifted_entity], "metrics": [drifted_metric]}
    )
    report = verify_catalog(
        drifting_document,
        _schema(),
        sqlite_runner(_registry(tmp_path), "shop"),
        "sqlite",
    )
    entity_claim = next(claim for claim in report.claims if claim.subject_kind == "entity")
    assert entity_claim.status == "stale"
    metric_claim = next(claim for claim in report.claims if claim.subject == "metric:order_total")
    # inferred facts have never claimed authority, so drift does not demote them
    assert metric_claim.status == "inferred"


def test_declared_fingerprint_drift_is_reported(tmp_path: Path) -> None:
    drifted = _document().model_copy(update={"schema_fingerprint": "sha256:deadbeef"})
    report = verify_catalog(
        drifted,
        _schema(),
        sqlite_runner(_registry(tmp_path), "shop"),
        "sqlite",
    )
    assert report.fingerprint_status == "stale"
    assert report.live_schema_fingerprint == schema_fingerprint(_schema())


def test_market_data_seed_is_governed() -> None:
    path = Path("data/semantic_catalogs/market_data.json")
    document = CatalogDocument.model_validate_json(path.read_text())
    assert document.metadata_status == "reviewed"
    assert document.entities[0].grain.status == "approved"
    assert document.entities[0].grain.approval is not None
    assert all(metric.status == "inferred" for metric in document.metrics)
    assert all(metric.approval is None for metric in document.metrics)
    assert document.source_db_id == "market_data"


def test_store_roundtrip_and_missing(tmp_path: Path) -> None:
    root = tmp_path / "semantic_catalogs"
    root.mkdir()
    store = SemanticCatalogStore(root)
    assert store.load("shop") is None
    document = _document()
    (root / "shop.json").write_text(document.model_dump_json())
    loaded = store.load("shop")
    assert loaded is not None
    assert loaded == document
    assert SemanticCatalogStore(None).load("shop") is None


def _api_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    (root / "shop").mkdir(parents=True)
    with sqlite3.connect(root / "shop" / "shop.sqlite") as connection:
        connection.executescript(_SHOP_SCHEMA)
    catalog_root = root / "semantic_catalogs"
    catalog_root.mkdir()
    (catalog_root / "shop.json").write_text(json.dumps(_document().model_dump(mode="json")))
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(root))
    monkeypatch.setenv("TEXT2SQL_SEMANTIC_CATALOG_ROOT", str(catalog_root))
    monkeypatch.delenv("TEXT2SQL_POSTGRES_DATABASES", raising=False)
    return root


def test_semantic_catalog_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _api_environment(tmp_path, monkeypatch)
    client = TestClient(create_app())

    response = client.get("/api/databases/shop/semantic-catalog")
    assert response.status_code == 200
    payload = response.json()
    assert payload["db_id"] == "shop"
    assert payload["fingerprint_status"] == "observed"  # seed declares no fingerprint yet
    assert payload["entities"][0]["live_status"] == "reviewed"
    assert payload["metrics"][0]["declared_status"] == "inferred"
    assert "dsn" not in response.text.casefold()
    assert "postgres://" not in response.text.casefold()

    verification = client.get("/api/databases/shop/semantic-catalog/verification")
    assert verification.status_code == 200
    report = verification.json()
    subjects = {claim["subject"] for claim in report["claims"]}
    assert "orders_fact.grain" in subjects
    assert "metric:order_total" in subjects
    assert isinstance(report["summary"], dict)

    missing_db = client.get("/api/databases/nope/semantic-catalog")
    assert missing_db.status_code == 404
    missing_catalog = client.get("/api/databases/books/semantic-catalog")
    assert missing_catalog.status_code == 404
