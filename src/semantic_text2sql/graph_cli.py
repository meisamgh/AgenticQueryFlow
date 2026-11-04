"""Build/validate offline semantic graphs from exported metadata, without database queries."""

from __future__ import annotations

import argparse
from pathlib import Path

from semantic_text2sql.glossary import BusinessGlossary
from semantic_text2sql.models import DatabaseProfile, SchemaInfo
from semantic_text2sql.semantic_catalog import CatalogDocument
from semantic_text2sql.semantic_graph import SemanticGraphStore, compile_graph, graph_identity


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["build", "validate"])
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--glossary", type=Path)
    parser.add_argument("--root", type=Path, default=Path("graphs"))
    args = parser.parse_args()
    try:
        schema = SchemaInfo.model_validate_json(args.schema.read_text())
        catalog = (
            CatalogDocument.model_validate_json(args.catalog.read_text()) if args.catalog else None
        )
        profile = (
            DatabaseProfile.model_validate_json(args.profile.read_text()) if args.profile else None
        )
        glossary = (
            BusinessGlossary.model_validate_json(args.glossary.read_text())
            if args.glossary
            else None
        )
        store = SemanticGraphStore(args.root)
        if args.action == "build":
            print(store.save(compile_graph(schema, catalog, profile, glossary)))
        elif store.load(schema.db_id, identity=graph_identity(schema, catalog, profile, glossary)):
            print("Compatible graph snapshot")
        else:
            parser.exit(1, "Graph is missing, corrupted or stale; explicitly rebuild.\n")
    except (OSError, ValueError):
        parser.exit(1, "Invalid or unavailable metadata; no graph accepted.\n")


if __name__ == "__main__":
    main()
