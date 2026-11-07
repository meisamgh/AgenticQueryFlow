"""A profile for another physical dialect must not ground this database."""
from pathlib import Path

import pytest

from semantic_text2sql.models import DatabaseProfile, SchemaInfo
from semantic_text2sql.profiling import ProfileStore
from semantic_text2sql.semantic_graph import compile_graph


def test_profile_filename_cannot_override_embedded_dialect(tmp_path: Path) -> None:
    wrong = DatabaseProfile(db_id="shop", dialect="sqlite", profiled_at="2026-01-01", columns=[])
    (tmp_path / "postgres__shop.json").write_text(wrong.model_dump_json())
    assert ProfileStore(tmp_path).load("postgres", "shop") is None


def test_graph_rejects_same_id_profile_for_another_engine() -> None:
    schema = SchemaInfo(db_id="shop", dialect="postgres", tables=[])
    wrong = DatabaseProfile(db_id="shop", dialect="sqlite", profiled_at="2026-01-01", columns=[])
    with pytest.raises(ValueError, match="[Dd]ialect"):
        compile_graph(schema, profile=wrong)
