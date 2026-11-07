"""Regression coverage for sidecar database and path boundaries."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from semantic_text2sql.api import create_app
from semantic_text2sql.glossary import GlossaryStore
from semantic_text2sql.models import DatabaseProfile, RelationshipProfile
from semantic_text2sql.profiling import ProfileStore
from semantic_text2sql.semantic_catalog import SemanticCatalogStore
from test_semantic_catalog import _document


def _glossary(db_id: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": "1",
        "source": "regression",
        "precedence": ["question"],
        "terms": [{"term": "identity-marker", "definition": "regression"}],
    }
    if db_id is not None:
        payload["db_id"] = db_id
    return payload


def test_unconfigured_database_sidecar_routes_are_404(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    database_root = tmp_path / "databases"
    glossary_root = tmp_path / "glossaries"
    catalog_root = tmp_path / "catalogs"
    profile_root = tmp_path / "profiles"
    for directory in (database_root, glossary_root, catalog_root, profile_root):
        directory.mkdir()
    (glossary_root / "secret.json").write_text(json.dumps(_glossary("secret")))
    catalog = _document().model_dump(mode="json")
    catalog.update({"id": "secret", "source_db_id": "secret"})
    (catalog_root / "secret.json").write_text(json.dumps(catalog))
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(database_root))
    monkeypatch.setenv("TEXT2SQL_GLOSSARY_ROOT", str(glossary_root))
    monkeypatch.setenv("TEXT2SQL_SEMANTIC_CATALOG_ROOT", str(catalog_root))
    monkeypatch.setenv("TEXT2SQL_PROFILE_ROOT", str(profile_root))
    monkeypatch.delenv("TEXT2SQL_POSTGRES_DATABASES", raising=False)

    client = TestClient(create_app())
    paths = [
        "/api/databases/secret/schema",
        "/api/databases/secret/glossary",
        "/api/databases/secret/semantic-catalog",
        "/api/databases/secret/semantic-catalog/verification",
        "/api/databases/secret/analytics-capabilities",
        "/api/databases/secret/filter-values?table=orders&column=status",
    ]
    statuses = {path: client.get(path).status_code for path in paths}

    assert statuses == {path: 404 for path in paths}


def test_sidecar_identity_mismatch_is_absent_but_missing_identity_loads(
    tmp_path: Path,
) -> None:
    glossary_root = tmp_path / "glossaries"
    glossary_root.mkdir()
    glossary_path = glossary_root / "shop.json"
    glossary_path.write_text(json.dumps(_glossary("other")))
    assert GlossaryStore(glossary_root).load("shop") is None
    glossary_path.write_text(json.dumps(_glossary(None)))
    glossary = GlossaryStore(glossary_root).load("shop")
    assert glossary is not None and glossary.db_id == "shop"

    catalog_root = tmp_path / "catalogs"
    catalog_root.mkdir()
    catalog_path = catalog_root / "shop.json"
    catalog = _document().model_dump(mode="json")
    catalog["source_db_id"] = "other"
    catalog_path.write_text(json.dumps(catalog))
    assert SemanticCatalogStore(catalog_root).load("shop") is None
    catalog.pop("source_db_id")
    catalog_path.write_text(json.dumps(catalog))
    document = SemanticCatalogStore(catalog_root).load("shop")
    assert document is not None and document.source_db_id == "shop"

    profile_root = tmp_path / "profiles"
    profile_root.mkdir()
    profile_path = profile_root / "sqlite__shop.json"
    profile = DatabaseProfile(
        db_id="other", dialect="sqlite", profiled_at="2026-01-01", columns=[],
    )
    profile_path.write_text(profile.model_dump_json())
    assert ProfileStore(profile_root).load("sqlite", "shop") is None
    payload = profile.model_dump(mode="json")
    payload.pop("db_id")
    profile_path.write_text(json.dumps(payload))
    loaded = ProfileStore(profile_root).load("sqlite", "shop")
    assert loaded is not None and loaded.db_id == "shop"


def test_sidecar_names_cannot_escape_the_store_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_glossary(None)))
    glossary_root = tmp_path / "glossaries"
    glossary_root.mkdir()
    assert GlossaryStore(glossary_root).load("../outside") is None

    catalog_root = tmp_path / "catalogs"
    catalog_root.mkdir()
    outside.write_text(json.dumps(_document().model_dump(mode="json")))
    assert SemanticCatalogStore(catalog_root).load("../outside") is None

    profile_root = tmp_path / "profiles"
    profile_root.mkdir()
    profile = DatabaseProfile(
        db_id="outside", dialect="sqlite", profiled_at="2026-01-01", columns=[],
    )
    outside.write_text(profile.model_dump_json())
    assert ProfileStore(profile_root).load("sqlite", "../../../outside") is None


def test_configured_database_does_not_serve_mismatched_sidecars(
    registry: Any, tmp_path: Path, monkeypatch: Any,
) -> None:
    glossary_root = tmp_path / "glossaries"
    catalog_root = tmp_path / "catalogs"
    profile_root = tmp_path / "profiles"
    for directory in (glossary_root, catalog_root, profile_root):
        directory.mkdir()
    (glossary_root / "shop.json").write_text(json.dumps(_glossary("other")))
    catalog = _document().model_dump(mode="json")
    catalog["source_db_id"] = "other"
    (catalog_root / "shop.json").write_text(json.dumps(catalog))
    profile = DatabaseProfile(
        db_id="other", dialect="sqlite", profiled_at="2026-01-01", columns=[],
        relationships=[
            RelationshipProfile(
                parent_table="customers", parent_column="country",
                child_table="orders", child_column="status",
                type="ONE_TO_MANY", parent_key_unique=False, child_key_unique=False,
                inferred=True, confidence=0.5,
            ),
        ],
    )
    (profile_root / "sqlite__shop.json").write_text(profile.model_dump_json())
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    monkeypatch.setenv("TEXT2SQL_GLOSSARY_ROOT", str(glossary_root))
    monkeypatch.setenv("TEXT2SQL_SEMANTIC_CATALOG_ROOT", str(catalog_root))
    monkeypatch.setenv("TEXT2SQL_PROFILE_ROOT", str(profile_root))
    client = TestClient(create_app())

    assert client.get("/api/databases/shop/glossary").json() == []
    assert client.get("/api/databases/shop/semantic-catalog").status_code == 404
    assert "profile_inference" not in {
        item["source"] for item in client.get("/api/databases/shop/schema").json()["relationships"]
    }
