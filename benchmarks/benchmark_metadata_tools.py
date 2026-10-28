"""Compare one two-tool metadata turn across revisions without network or SQL models.

Run with PYTHONPATH pointing to the revision's ``src`` directory. The benchmark
uses the same scripted Top-layer decisions and a real temporary SQLite schema.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import statistics
import tempfile
from pathlib import Path
from time import perf_counter
from typing import Any

from semantic_text2sql.conversational_agent import ConversationalAnalyticsAgent
from semantic_text2sql.conversational_bridge import ConversationalAnalyticsBridge
from semantic_text2sql.database import DatabaseRegistry
from semantic_text2sql.glossary import GlossaryStore
from semantic_text2sql.hybrid_retrieval import HybridSchemaRetriever
from semantic_text2sql.models import ChatRequest, ChatResponse, TokenUsage
from semantic_text2sql.profiling import ProfileStore


class Encoder:
    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


class ScriptedTopModel:
    def __init__(self) -> None:
        self.calls = 0
        self.prompt_chars = 0

    async def complete_detailed(self, model: str, prompt: str) -> tuple[str, TokenUsage]:
        del model
        self.prompt_chars += len(prompt)
        decisions: list[dict[str, Any]] = [
            {"action": "CALL_TOOL", "intent": "EXPLORE_SCHEMA", "tool": "list_tables",
             "instruction": "List available tables", "answer": None},
            {"action": "CALL_TOOL", "intent": "EXPLORE_SCHEMA", "tool": "search_schema",
             "instruction": "Which columns are in customers?", "answer": None},
            {"action": "ANSWER", "intent": "EXPLORE_SCHEMA", "tool": None,
             "instruction": None, "answer": "The customers table has an ID and a country."},
        ]
        choice = decisions[self.calls]
        self.calls += 1
        return json.dumps(choice), TokenUsage(input_tokens=10, output_tokens=5)


async def measure(repetitions: int = 30) -> dict[str, float | int]:
    with tempfile.TemporaryDirectory() as root:
        base = Path(root)
        folder = base / "bench"
        folder.mkdir()
        with sqlite3.connect(folder / "bench.sqlite") as connection:
            connection.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, country TEXT)")
            connection.execute("INSERT INTO customers VALUES (1, 'Germany')")
        registry = DatabaseRegistry(base)
        original_inspect = registry.inspect
        inspections = 0

        def inspect(db_id: str) -> Any:
            nonlocal inspections
            inspections += 1
            return original_inspect(db_id)

        registry.inspect = inspect  # type: ignore[method-assign]

        async def no_sql(request: ChatRequest) -> ChatResponse:
            raise AssertionError(f"Metadata turn must not run SQL: {request.message}")

        bridge = ConversationalAnalyticsBridge(
            database=registry, postgres=None,
            profiles=ProfileStore(base / "profiles"),
            glossaries=GlossaryStore(base / "glossaries"),
            retriever=HybridSchemaRetriever(Encoder()),  # type: ignore[arg-type]
            sql_handler=no_sql,
        )
        durations: list[float] = []
        database_calls: list[int] = []
        prompt_lengths: list[int] = []
        for index in range(repetitions):
            model = ScriptedTopModel()
            agent = ConversationalAnalyticsAgent(
                bridge=bridge, completers={"agentrouter": model},
                get_state=lambda session_id: None,
                get_last_response=lambda session_id, db_id: None,
                sql_handler=no_sql,
            )
            before = inspections
            started = perf_counter()
            response = await agent.run(ChatRequest(
                session_id=f"metadata-{index}", db_id="bench",
                message="Which customer data is available?",
            ))
            durations.append((perf_counter() - started) * 1_000)
            database_calls.append(inspections - before)
            prompt_lengths.append(model.prompt_chars)
            assert "customers" in response.message and model.calls == 3
        ordered = sorted(durations)
        return {
            "repetitions": repetitions,
            "p50_ms": round(statistics.median(durations), 3),
            "p95_ms": round(ordered[max(0, int(0.95 * repetitions) - 1)], 3),
            "schema_inspections_per_turn": database_calls[-1],
            "model_calls_per_turn": 3,
            "mock_input_tokens_per_turn": 30,
            "mock_output_tokens_per_turn": 15,
            "prompt_chars_per_turn": prompt_lengths[-1],
            "governed_sql_calls_per_turn": 0,
        }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(measure()), sort_keys=True))
