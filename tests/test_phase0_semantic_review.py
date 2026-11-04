"""Offline evaluation-tool tests; not production routing or a business correctness oracle."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_benchmark(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "benchmarks" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


review = load_benchmark("phase0_semantic_review")
live = load_benchmark("phase0_live_checks")


@pytest.mark.parametrize(
    "predicted",
    [
        "SELECT x.id FROM a x JOIN b y ON y.id=x.id",
        "SELECT T.ID FROM a T INNER JOIN b U ON T.ID=U.ID",
        "WITH q AS (SELECT a.id FROM a JOIN b ON a.id=b.id) SELECT id FROM q",
    ],
)
def test_alias_and_direct_cte_lineage_are_normalized(predicted: str) -> None:
    comparison = review.compare_joins("SELECT a.id FROM a JOIN b ON a.id=b.id", predicted)
    assert comparison["status"] == "EXACT_MATCH"


def test_composite_join_conjuncts_preserve_every_predicate() -> None:
    gold = "SELECT a.id FROM a JOIN b ON a.id=b.id AND a.part=b.part"
    equivalent = "SELECT x.id FROM a x JOIN b y ON (y.part=x.part AND (y.id=x.id))"
    assert review.compare_joins(gold, equivalent)["status"] == "EXACT_MATCH"
    wrong = "SELECT a.id FROM a JOIN b ON a.id=b.part AND a.part=b.id"
    assert review.compare_joins(gold, wrong)["status"] == "DIFFERENT"


def test_missing_join_and_schema_qualification_do_not_match() -> None:
    gold = "SELECT a.id FROM main.a a JOIN main.b b ON a.id=b.id"
    predicted = "SELECT a.id FROM other.a a JOIN main.b b ON a.id=b.id"
    assert not review.compare_joins(gold, predicted)["physical_tables_equal"]
    assert review.compare_joins(gold, "SELECT id FROM main.a")["status"] == "DIFFERENT"
    assert review.compare_joins("SELECT id FROM a", "SELECT id FROM b")["status"] == "DIFFERENT"


def test_derived_join_lineage_is_unresolved_not_claimed_wrong() -> None:
    gold = "SELECT a.id FROM a JOIN b ON a.id=b.id"
    predicted = "WITH q AS (SELECT SUM(id) AS n FROM a) SELECT b.id FROM b JOIN q ON b.id=q.n"
    assert review.compare_joins(gold, predicted)["status"] == "UNRESOLVED"
    assert review.compare_joins(gold, "SELECT ???")["status"] == "UNRESOLVED"


@pytest.mark.parametrize("a,b,expected", [(1, 1, 1.0), (8, 0, 0.0078125), (0, 0, 1.0)])
def test_mcnemar_uses_discordant_paired_counts(a: int, b: int, expected: float) -> None:
    rows = [{"retrieval": {"correct": True}, "model1": {"correct": False}}] * a + [
        {"retrieval": {"correct": False}, "model1": {"correct": True}}
    ] * b
    result = review.paired_counts(rows)
    assert result["only_retrieval"] == a and result["only_model1"] == b
    assert result["mcnemar_exact_two_sided_p"] == pytest.approx(expected)


def test_fixed_manifest_is_complete_disjoint_and_discloses_exposure() -> None:
    artifact = json.loads((ROOT / "benchmarks/results/debit_card_model1_ab_20.json").read_text())
    split = json.loads((ROOT / "benchmarks/data/debit20_phase0_split.json").read_text())
    computed = review.frozen_split([row["question_id"] for row in artifact["results"]])
    assert split["dev_ids"] == computed["dev_ids"]
    assert split["heldout_ids"] == computed["heldout_ids"]
    assert not set(split["dev_ids"]) & set(split["heldout_ids"])
    assert split["catalog_authoring_case_ids"] == []
    assert "not a pristine holdout" in split["warning"]
    invalid = {**split, "catalog_authoring_case_ids": [split["heldout_ids"][0]]}
    with pytest.raises(ValueError, match="dev only"):
        review.analyze(artifact, [], {}, invalid)


def test_saved_report_exposes_unknown_stage_causes_and_history_overlap() -> None:
    artifact = json.loads((ROOT / "benchmarks/results/debit_card_model1_ab_20.json").read_text())
    history = json.loads((ROOT / "benchmarks/data/bird_history_seed42_400.json").read_text())
    annotations = json.loads((ROOT / "benchmarks/data/debit20_phase0_annotations.json").read_text())
    result = review.analyze(artifact, history, annotations)
    counts = result["paired"]["all"]
    assert [
        counts[key] for key in ("both_correct", "only_retrieval", "only_model1", "neither")
    ] == [5, 1, 1, 13]
    assert result["taxonomy"]["retrieval"]["status"] == "UNRESOLVED"
    assert result["false_repairs"]["count"] is None
    assert result["history"]["records"] == 399
    assert len(result["history"]["question_overlap_ids"]) == 16


def test_readonly_recheck_rejects_writes_and_preserves_truncation(tmp_path: Path) -> None:
    directory = tmp_path / "debit_card_specializing"
    directory.mkdir()
    path = directory / "debit_card_specializing.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE items (v INTEGER PRIMARY KEY)")
        connection.executemany("INSERT INTO items VALUES (?)", [(i,) for i in range(600)])
    artifact = {
        "results": [
            {
                "question_id": 1,
                "gold_sql": "SELECT v FROM items",
                "retrieval": {"sql": "SELECT v FROM items WHERE v < 3", "correct": False},
                "model1": {"sql": "DELETE FROM items", "correct": False},
            }
        ]
    }
    result = live.recheck_saved_sql(tmp_path, artifact)
    case = result["cases"][0]
    assert case["checks"]["gold"]["status"] == "TRUNCATED"
    assert case["reference_match"]["retrieval"] is False
    assert case["comparison_proof"]["retrieval"] == "DIFFERENT_ROW_COUNT_BY_TRUNCATION_LOWER_BOUND"
    assert case["checks"]["model1"]["status"] == "REJECTED"
    assert case["reference_match"]["model1"] is None
    assert result["sql_execution_calls"] == 2 and result["model_calls"] == 0
    assert result["raw_rows_persisted"] is False
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 600
