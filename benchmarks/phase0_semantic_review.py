"""Artifact-only Phase 0: no SQL execution, inference, catalog authoring or runtime changes."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import Scope, traverse_scope


def relation(table: exp.Table) -> str:
    return ".".join(part.name.casefold() for part in table.parts)


def source_tables(source: exp.Table | Scope, seen: frozenset[int] = frozenset()) -> set[str]:
    if isinstance(source, exp.Table):
        return {relation(source)}
    if id(source) in seen:
        return set()
    return set().union(
        *(source_tables(item, seen | {id(source)}) for _, item in source.selected_sources.values())
    )


def column_lineage(
    column: exp.Column, scope: Scope, seen: frozenset[tuple[int, str]] = frozenset()
) -> str | None:
    key = (id(scope), column.sql())
    if key in seen:
        return None
    selected = scope.selected_sources
    source = (
        selected.get(column.table, (None, None))[1]
        if column.table
        else (next(iter(selected.values()))[1] if len(selected) == 1 else None)
    )
    if isinstance(source, exp.Table):
        return f"{relation(source)}.{column.name.casefold()}"
    if isinstance(source, Scope) and isinstance(source.expression, exp.Select):
        matches = [
            item.unalias()
            for item in source.expression.expressions
            if item.alias_or_name.casefold() == column.name.casefold()
        ]
        if len(matches) == 1 and isinstance(matches[0], exp.Column):
            return column_lineage(matches[0], source, seen | {key})
        # Never guess lineage through AVG/SUM/window expressions or ambiguous projections.
    return None


def canonical_predicate(predicate: exp.Expression, scope: Scope) -> tuple[str, bool]:
    expression = predicate.copy()
    unresolved = False
    for column in list(expression.find_all(exp.Column)):
        mapped = column_lineage(column, scope)
        if mapped is None:
            unresolved = True
            mapped = f"unresolved:{column.sql().casefold()}"
        column.replace(exp.Var(this=mapped))
    for equality in expression.find_all(exp.EQ):
        if equality.this.sql() > equality.expression.sql():
            left, right = equality.this.copy(), equality.expression.copy()
            equality.set("this", right)
            equality.set("expression", left)
    return expression.sql(dialect="sqlite", normalize=True), unresolved


def conjuncts(expression: exp.Expression) -> list[exp.Expression]:
    if isinstance(expression, exp.Paren):
        return conjuncts(expression.this)
    if isinstance(expression, exp.And):
        return [*conjuncts(expression.this), *conjuncts(expression.expression)]
    return [expression]


def join_signature(sql: str) -> dict[str, Any]:
    try:
        root = normalize_identifiers(parse_one(sql, read="sqlite"), dialect="sqlite")
        physical: set[str] = set()
        joined: set[str] = set()
        predicates: set[str] = set()
        unresolved: set[str] = set()
        kinds: list[str] = []
        for scope in traverse_scope(root):
            sources = scope.selected_sources
            for _, source in sources.values():
                physical.update(source_tables(source))
            for join in scope.expression.args.get("joins", []):
                for _, source in sources.values():
                    joined.update(source_tables(source))
                kinds.append(f"{join.side or 'INNER'}:{join.kind or 'JOIN'}")
                condition = join.args.get("on")
                if condition is not None:
                    for predicate in conjuncts(condition):
                        canonical, unknown = canonical_predicate(predicate, scope)
                        predicates.add(canonical)
                        if unknown:
                            unresolved.add(canonical)
                if join.args.get("using"):
                    unresolved.add("USING requires live schema disambiguation")
        return {
            "parse_status": "PARSED",
            "physical_tables": sorted(physical),
            "joined_tables": sorted(joined),
            "on_predicates": sorted(predicates),
            "join_kinds": sorted(kinds),
            "unresolved_predicates": sorted(unresolved),
        }
    except (SqlglotError, ValueError, AttributeError) as exc:
        return {"parse_status": "UNRESOLVED", "error_type": type(exc).__name__}


def compare_joins(gold_sql: str, predicted_sql: str) -> dict[str, Any]:
    gold, predicted = join_signature(gold_sql), join_signature(predicted_sql)
    if gold["parse_status"] != "PARSED" or predicted["parse_status"] != "PARSED":
        return {"status": "UNRESOLVED", "gold": gold, "predicted": predicted}
    tables_equal = gold["joined_tables"] == predicted["joined_tables"]
    on_equal = gold["on_predicates"] == predicted["on_predicates"]
    physical_equal = gold["physical_tables"] == predicted["physical_tables"]
    unknown = bool(gold["unresolved_predicates"] or predicted["unresolved_predicates"])
    return {
        "status": "UNRESOLVED"
        if unknown
        else "EXACT_MATCH"
        if tables_equal and on_equal and physical_equal
        else "DIFFERENT",
        "joined_tables_equal": tables_equal,
        "on_predicates_equal": on_equal,
        "physical_tables_equal": physical_equal,
        "gold": gold,
        "predicted": predicted,
        "meaning": "Structural agreement only; EXISTS/CTE rewrites can be equivalent.",
    }


def paired_counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    both = only_a = only_b = neither = 0
    for row in rows:
        a, b = row["retrieval"]["correct"], row["model1"]["correct"]
        both += bool(a and b)
        only_a += bool(a and not b)
        only_b += bool(b and not a)
        neither += bool(not a and not b)
    discordant = only_a + only_b
    probability = (
        min(
            1.0,
            2
            * sum(math.comb(discordant, k) for k in range(min(only_a, only_b) + 1))
            / 2**discordant,
        )
        if discordant
        else 1.0
    )
    return {
        "n": len(rows),
        "both_correct": both,
        "only_retrieval": only_a,
        "only_model1": only_b,
        "neither": neither,
        "discordant_pairs": discordant,
        "mcnemar_exact_two_sided_p": probability,
        "interpretation": "No evidence of a difference"
        if probability >= 0.05
        else "Discordance is asymmetric; assess benchmark validity before interpretation",
    }


def frozen_split(ids: list[int]) -> dict[str, Any]:
    ordered = sorted(
        ids,
        key=lambda value: hashlib.sha256(
            f"queryflow-phase0-2026-11-04:{value}".encode()
        ).hexdigest(),
    )
    return {
        "method": "sha256 of fixed namespace and case ID; independent of labels",
        "dev_ids": sorted(ordered[: len(ids) // 2]),
        "heldout_ids": sorted(ordered[len(ids) // 2 :]),
        "catalog_authoring_case_ids": [],
        "warning": (
            "Prospective split only: all 20 gold queries were previously inspected. "
            "Not a pristine holdout."
        ),
    }


def analyze(
    artifact: dict[str, Any],
    history: list[dict[str, Any]],
    annotations: dict[str, Any],
    split: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = artifact["results"]
    split = split or frozen_split([row["question_id"] for row in rows])
    dev, held = set(split["dev_ids"]), set(split["heldout_ids"])
    if dev & held or dev | held != {row["question_id"] for row in rows}:
        raise ValueError("Split must be a disjoint complete partition of this fixture")
    if set(split.get("catalog_authoring_case_ids", [])) - dev:
        raise ValueError("Catalog authoring IDs must belong to dev only")
    history_questions = {(row["db_id"], row["question"].strip().casefold()) for row in history}
    overlap = [
        row["question_id"]
        for row in rows
        if ("debit_card_specializing", row["question"].strip().casefold()) in history_questions
    ]
    cases = []
    for row in rows:
        cases.append(
            {
                "question_id": row["question_id"],
                "split": "dev" if row["question_id"] in split["dev_ids"] else "heldout",
                "historical_corpus_question_overlap": row["question_id"] in overlap,
                "assessment": annotations.get(str(row["question_id"])),
                "modes": {
                    mode: {
                        "stored_reference_match": row[mode]["correct"],
                        "stored_accepted": row[mode]["accepted"],
                        "join_comparison": compare_joins(row["gold_sql"], row[mode]["sql"]),
                    }
                    for mode in ("retrieval", "model1")
                },
            }
        )
    cohorts = {
        "all": rows,
        "dev": [row for row in rows if row["question_id"] in split["dev_ids"]],
        "heldout": [row for row in rows if row["question_id"] in split["heldout_ids"]],
    }
    join_counts = {
        mode: {
            status: sum(
                case["modes"][mode]["join_comparison"]["status"] == status for case in cases
            )
            for status in ("EXACT_MATCH", "DIFFERENT", "UNRESOLVED")
        }
        for mode in ("retrieval", "model1")
    }
    join_paired = {}
    for name, group in cohorts.items():
        ids = {row["question_id"] for row in group}
        known = [
            case
            for case in cases
            if case["question_id"] in ids
            and all(
                case["modes"][mode]["join_comparison"]["status"] != "UNRESOLVED"
                for mode in ("retrieval", "model1")
            )
        ]
        join_paired[name] = {
            **paired_counts(
                [
                    {
                        mode: {
                            "correct": case["modes"][mode]["join_comparison"]["status"]
                            == "EXACT_MATCH"
                        }
                        for mode in ("retrieval", "model1")
                    }
                    for case in known
                ]
            ),
            "unresolved_pairs": len(group) - len(known),
        }
    latency = {
        mode: {
            "p50_ms": statistics.median(row[mode]["latency_ms"] for row in rows),
            "p95_ms_nearest_rank": sorted(row[mode]["latency_ms"] for row in rows)[
                math.ceil(0.95 * len(rows)) - 1
            ],
            "status": "MEASURED_FROM_SAVED_ARTIFACT_NOT_RERUN",
        }
        for mode in ("retrieval", "model1")
    }
    failure_ids = sorted(
        row["question_id"]
        for row in rows
        if not row["retrieval"]["correct"] or not row["model1"]["correct"]
    )
    taxonomy = {
        category: {
            "case_ids": sorted(
                int(key) for key, value in annotations.items() if category in value["categories"]
            ),
            "status": "MEASURED_SAVED_SQL_DIFFERENCE",
            "note": "Reference divergence, not a proven causal pipeline-stage failure",
        }
        for category in ("joins", "metric_semantics", "filters_time", "sql_generation")
    }
    taxonomy["retrieval"] = {
        "confirmed_case_ids": [],
        "unresolved_case_ids": failure_ids,
        "status": "UNRESOLVED",
        "reason": "Selected counts are saved, not retrieved identifiers/prompts",
    }
    taxonomy["validation"] = {
        "case_ids": failure_ids,
        "status": "MEASURED_ACCEPTANCE_REFERENCE_GAP",
        "accepted_reference_mismatches": sum(
            row[mode]["accepted"] and not row[mode]["correct"]
            for row in rows
            for mode in ("retrieval", "model1")
        ),
        "note": (
            "Not evidence of failed SQL safety; reference validity/output shape confound grading"
        ),
    }
    taxonomy["recovery"] = {
        "confirmed_case_ids": [],
        "unresolved_case_ids": failure_ids,
        "status": "NOT_RUN",
        "reason": "No recovery traces or repaired SQL retained",
    }
    taxonomy["joins"]["structural_difference_case_ids"] = [
        case["question_id"]
        for case in cases
        if any(
            case["modes"][mode]["join_comparison"]["status"] == "DIFFERENT"
            for mode in ("retrieval", "model1")
        )
    ]
    taxonomy["joins"]["unresolved_case_ids"] = [
        case["question_id"]
        for case in cases
        if any(
            case["modes"][mode]["join_comparison"]["status"] == "UNRESOLVED"
            for mode in ("retrieval", "model1")
        )
    ]
    return {
        "scope": "PHASE_0_ONLY; no runtime changes or catalog authoring",
        "split": split,
        "paired": {name: paired_counts(group) for name, group in cohorts.items()},
        "join_structural_agreement": join_counts,
        "join_structural_paired": join_paired,
        "historical_latency": latency,
        "history": {
            "records": len(history),
            "databases": len({row["db_id"] for row in history}),
            "fields": sorted(set().union(*(set(row) for row in history))),
            "question_overlap_ids": overlap,
            "heldout_overlap_ids": sorted(set(overlap) & set(split["heldout_ids"])),
            "paired_predictions_available": False,
            "note": (
                "SQL labels are candidate references, not execution/business verification; "
                "success is set true by the builder."
            ),
        },
        "false_repairs": {
            "status": "NOT_RUN",
            "count": None,
            "reason": "Saved artifact has no recovery traces or repair outcomes",
        },
        "not_run": {
            "execution_recheck": "No optional existing database root was supplied to this analysis",
            "business_correctness": (
                "Reference definitions need adjudication; row agreement is not business truth"
            ),
            "current_latency_tokens_recovery": "No fresh model-generation/recovery benchmark run",
            "provider_credentials_validity": "No provider request attempted at the Phase 0 gate",
            "graph_vs_baseline_accuracy": "No paired graph-on predictions available",
        },
        "taxonomy": taxonomy,
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--split", type=Path, default=Path("benchmarks/data/debit20_phase0_split.json")
    )
    parser.add_argument(
        "--database-root",
        type=Path,
        help="Optional existing SQLite registry root; read-only saved-SQL recheck",
    )
    args = parser.parse_args()
    artifact_path = Path("benchmarks/results/debit_card_model1_ab_20.json")
    annotations_path = Path("benchmarks/data/debit20_phase0_annotations.json")
    report = analyze(
        json.loads(artifact_path.read_text()),
        json.loads(Path("benchmarks/data/bird_history_seed42_400.json").read_text()),
        json.loads(annotations_path.read_text()),
        json.loads(args.split.read_text()),
    )
    report["source_sha256"] = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    report["split_sha256"] = hashlib.sha256(args.split.read_bytes()).hexdigest()
    report["annotations_sha256"] = hashlib.sha256(annotations_path.read_bytes()).hexdigest()
    if args.database_root:
        from phase0_live_checks import measured_metadata_graph, recheck_saved_sql

        report["saved_sql_execution_recheck"] = recheck_saved_sql(
            args.database_root, json.loads(artifact_path.read_text())
        )
        report["largest_available_schema"] = measured_metadata_graph(args.database_root)
        if report["saved_sql_execution_recheck"]["status"] == "MEASURED":
            report["not_run"].pop("execution_recheck", None)
            report["not_run"]["current_latency_tokens_recovery"] = (
                "Saved SQL was re-executed, not regenerated; "
                "a new paired model/recovery run is needed"
            )
            report["execution_recheck_paired"] = {}
            for cohort, ids in (
                ("all", None),
                ("dev", set(report["split"]["dev_ids"])),
                ("heldout", set(report["split"]["heldout_ids"])),
            ):
                live_rows = [
                    row
                    for row in report["saved_sql_execution_recheck"]["cases"]
                    if ids is None or row["question_id"] in ids
                ]
                resolved = [
                    {
                        mode: {"correct": row["reference_match"][mode]}
                        for mode in ("retrieval", "model1")
                    }
                    for row in live_rows
                    if all(value is not None for value in row["reference_match"].values())
                ]
                report["execution_recheck_paired"][cohort] = {
                    **paired_counts(resolved),
                    "unresolved_pairs": len(live_rows) - len(resolved),
                }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "paired": report["paired"],
                "split": report["split"],
                "joins": report["join_structural_agreement"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
