"""Paired local audit reproductions; no real models, business rows or network.

Run: uv run python benchmarks/benchmark_audit_paths.py
The HEAD versions of these paths were unchanged by the pre-existing worktree
cleanup. Reports synthetic timings, not production latency or SQL accuracy.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter, sleep
from types import ModuleType
from typing import Any

from semantic_text2sql import hybrid_retrieval, recovery, validator
from semantic_text2sql.database import DatabaseRegistry
from semantic_text2sql.models import TokenUsage
from semantic_text2sql.runtime import RequestBudget


def baseline(name: str) -> ModuleType:
    text = subprocess.check_output(
        ["git", "show", f"HEAD:src/semantic_text2sql/{name}.py"], text=True,
    )
    module = ModuleType(f"audit_baseline_{name}")
    sys.modules[module.__name__] = module
    exec(compile(text, f"HEAD:{name}.py", "exec"), module.__dict__)
    return module


def stats(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {"p50_ms": round(median(values), 4),
            "p95_ms": round(ordered[max(0, int(len(values) * .95) - 1)], 4)}


def dense_fallback(module: Any) -> dict[str, object]:
    measured = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        for _ in range(10):
            encoder = module.FastEmbedEncoder()
            encoder.disable("Synthetic initialization timeout")
            encoder._lock.acquire()

            def call(encoder: Any = encoder) -> float:
                started = perf_counter()
                with suppress(RuntimeError):
                    encoder.encode(["synthetic"])
                return (perf_counter() - started) * 1000

            future = pool.submit(call)
            sleep(.03)
            encoder._lock.release()
            measured.append(future.result())
    return {**stats(measured), "cases": len(measured), "model_calls": 0, "db_calls": 0}


def failed_recovery(module: Any, registry: DatabaseRegistry) -> dict[str, object]:
    timings, counts = [], []

    class Scripted:
        calls = 0

        async def complete_detailed(self, *args: object) -> tuple[str, TokenUsage]:
            self.calls += 1
            await asyncio.sleep(.005)
            raise TypeError("Synthetic parser failure")

    for _ in range(10):
        reasoner, budget = Scripted(), RequestBudget(10)
        tools = module.RecoveryTools(
            registry, "shop", registry.inspect("shop"), None, budget=budget,
        )
        started = perf_counter()
        asyncio.run(module.RecoveryCoordinator(tools).ainvestigate(
            question="Check the result", failed_sql="SELECT SUM(amount) FROM orders",
            failure_code="NULL_RESULT", failure_message="Unexpected NULL",
            mode="NULL_RESULT", allowed_tables=["orders"], completer=reasoner,
            provider="groq", model="scripted", budget=budget,
        ))
        timings.append((perf_counter() - started) * 1000)
        counts.append(reasoner.calls)
        assert budget.model_calls == reasoner.calls
    return {**stats(timings), "cases": len(timings), "model_calls_each": counts,
            "db_calls": budget.database_calls}


def authorization(module: Any, registry: DatabaseRegistry) -> dict[str, object]:
    timings = []
    schema = registry.inspect("shop")
    for _ in range(101):
        started = perf_counter()
        result = module.validate_sql("SELECT amount FROM orders", schema,
                                     allowed_columns={"orders": {"amount"}})
        timings.append((perf_counter() - started) * 1000)
        assert result.valid
    return {**stats(timings), "cases": len(timings), "model_calls": 0, "db_calls": 0}


def main() -> None:
    with TemporaryDirectory(prefix="queryflow-audit-") as directory:
        folder = Path(directory) / "shop"
        folder.mkdir()
        with sqlite3.connect(folder / "shop.sqlite") as connection:
            connection.execute("CREATE TABLE orders(amount REAL)")
        registry = DatabaseRegistry(Path(directory))
        output: dict[str, object] = {"baseline": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True,
        ).strip(), "kind": "synthetic/offline; not production"}
        for label, operation, old, new in [
            ("disabled_dense", dense_fallback, baseline("hybrid_retrieval"), hybrid_retrieval),
            ("failed_recovery", lambda m: failed_recovery(m, registry), baseline("recovery"),
             recovery),
            ("column_authorization", lambda m: authorization(m, registry), baseline("validator"),
             validator),
        ]:
            output[label] = {"before": operation(old), "after": operation(new)}
        print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
