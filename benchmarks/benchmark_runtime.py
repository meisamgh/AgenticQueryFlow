"""Reproducible local cold/warm API and transport benchmark (no external model).

Run: .venv/bin/python benchmarks/benchmark_runtime.py
The dense encoder is intentionally forced into BM25 fallback, so these timings
measure local orchestration rather than model download or provider latency.
"""

from __future__ import annotations

import os
import sqlite3
import statistics
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from semantic_text2sql.agent import TextToSQLAgent
from semantic_text2sql.api import create_app
from semantic_text2sql.database import DatabaseRegistry
from semantic_text2sql.hybrid_retrieval import FastEmbedEncoder
from semantic_text2sql.run_history import RunHistoryStore


class FixedModel:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, **kwargs: Any) -> tuple[str, int]:
        self.calls += 1
        return "SELECT id, label, amount FROM orders ORDER BY id", 1


def main() -> None:
    with tempfile.TemporaryDirectory() as root:
        base = Path(root)
        db_dir = base / "bench"
        db_dir.mkdir()
        with sqlite3.connect(db_dir / "bench.sqlite") as connection:
            connection.execute(
                "CREATE TABLE orders (id INTEGER PRIMARY KEY, label TEXT, amount REAL)"
            )
            connection.executemany(
                "INSERT INTO orders VALUES (?, ?, ?)",
                [(i, f"label-{i}", i * 1.25) for i in range(500)],
            )
        os.environ["TEXT2SQL_DATABASE_ROOT"] = root
        os.environ["TEXT2SQL_PROFILE_ROOT"] = str(base / "profiles")
        os.environ["TEXT2SQL_GLOSSARY_ROOT"] = str(base / "glossaries")
        os.environ["TEXT2SQL_EMBEDDING_PRELOAD"] = "false"
        os.environ["TEXT2SQL_GZIP_ENABLED"] = "true"

        def no_dense(self: FastEmbedEncoder, texts: Any) -> list[list[float]]:
            raise RuntimeError("Measured BM25 fallback")

        FastEmbedEncoder.encode = no_dense  # type: ignore[method-assign]
        registry = DatabaseRegistry(base)
        model = FixedModel()
        app = create_app(TextToSQLAgent(registry, model), run_history=RunHistoryStore(None))
        tracemalloc.start()
        with TestClient(app) as client:
            for label, encoding, repetitions in (
                ("cold", "identity", 1),
                ("warm", "identity", 10),
                ("gzip", "gzip", 10),
            ):
                observations: list[dict[str, float | int]] = []
                for iteration in range(repetitions):
                    before_calls = model.calls
                    started = time.perf_counter()
                    response = client.post(
                        "/api/chat",
                        json={
                            "session_id": f"{label}-{iteration}",
                            "db_id": "bench",
                            "model": "test-model",
                            "message": "Show all orders",
                            "max_rows": 500,
                        },
                        headers={"Accept-Encoding": encoding},
                    )
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    body = response.json()
                    observations.append(
                        {
                            "status": response.status_code,
                            "latency_ms": elapsed_ms,
                            "model_calls": model.calls - before_calls,
                            "db_calls": sum(
                                item["kind"] == "DATABASE" for item in body.get("call_ledger", [])
                            ),
                            "response_bytes": int(
                                response.headers.get("content-length", len(response.content))
                            ),
                            "decoded_bytes": len(response.content),
                        }
                    )
                latencies = sorted(float(item["latency_ms"]) for item in observations)
                print(
                    {
                        "mode": label,
                        "requests": repetitions,
                        "status": int(observations[-1]["status"]),
                        "median_latency_ms": round(statistics.median(latencies), 2),
                        "p95_latency_ms": round(
                            latencies[max(0, int(0.95 * len(latencies)) - 1)], 2
                        ),
                        "model_calls_per_request": int(observations[-1]["model_calls"]),
                        "db_ledger_calls_per_request": int(observations[-1]["db_calls"]),
                        "median_transfer_bytes": int(
                            statistics.median(item["response_bytes"] for item in observations)
                        ),
                        "decoded_bytes": int(observations[-1]["decoded_bytes"]),
                        "peak_memory_kib": round(tracemalloc.get_traced_memory()[1] / 1024, 1),
                    }
                )


if __name__ == "__main__":
    main()
