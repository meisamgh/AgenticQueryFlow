"""Explicit offline profiling for configured remote connections; no model calls."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from semantic_text2sql.database import DatabaseError
from semantic_text2sql.external_connections import external_connections_from_environment
from semantic_text2sql.postgres import postgres_databases_from_environment
from semantic_text2sql.profiling import ProfileStore, profile_database
from semantic_text2sql.remote_databases import RemoteDatabaseRegistry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-id", required=True)
    parser.add_argument("--sample-limit", type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.sample_limit <= 1000:
        parser.error("sample-limit must be between 1 and 1000")
    registry = RemoteDatabaseRegistry(
        postgres_databases_from_environment(), external_connections_from_environment(),
    )
    if args.db_id not in registry.configured_ids():
        parser.error("The database ID is not configured")
    try:
        profile = profile_database(
            registry, args.db_id, registry.dialect_for(args.db_id),
            sample_limit=args.sample_limit, top_limit=5,
        )
        path = ProfileStore(Path(os.environ.get("TEXT2SQL_PROFILE_ROOT", "profiles"))).save(profile)
        print(path)
    except DatabaseError as exc:
        parser.exit(1, f"Profiling failed ({exc.code}); check connection access and limits.\n")
    finally:
        registry.close()


if __name__ == "__main__":
    main()
