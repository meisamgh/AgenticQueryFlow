"""Measure local assistant orchestration, excluding network/model/database time.

Run: .venv/bin/python benchmarks/benchmark_assistant.py
This is not a live-provider latency or analytical-accuracy benchmark.
"""

from __future__ import annotations

import asyncio
import json
from statistics import median
from time import perf_counter
from typing import Any, cast

from semantic_text2sql.conversational_agent import ConversationalAnalyticsAgent
from semantic_text2sql.conversational_bridge import ConversationalAnalyticsBridge
from semantic_text2sql.models import (
    ChatRequest,
    ChatResponse,
    GenerateResponse,
    StrategyHints,
    TokenUsage,
)
from semantic_text2sql.result_cache import RecentResult


class MockCompleter:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.calls = 0

    async def complete_detailed(self, model: str, prompt: str) -> tuple[str, TokenUsage]:
        self.calls += 1
        if self.kind == "greeting":
            answer: dict[str, Any] = {
                "action": "ANSWER", "intent": "DIRECT_ANSWER", "tool": None,
                "instruction": None, "answer": "Hello. Ask me about your data.",
            }
        elif self.calls == 1:
            answer: dict[str, Any] = {
                "action": "CALL_TOOL", "intent": "NEW_QUERY", "tool": "ask_data",
                "instruction": "Count orders", "answer": None,
            }
        else:
            answer = {
                "action": "ANSWER", "intent": "NEW_QUERY", "tool": None,
                "instruction": None, "answer": "The verified result contains one row.",
            }
        return json.dumps(answer), TokenUsage(input_tokens=10, output_tokens=5)


def accepted() -> ChatResponse:
    return ChatResponse(
        session_id="benchmark",
        operation="NEW_QUERY",
        resolved_question="Count orders",
        message="The query ran.",
        generation=GenerateResponse(
            db_id="benchmark",
            question="Count orders",
            provider="agentrouter",
            model="mock",
            strategy=StrategyHints(mode="exact"),
            sql="SELECT COUNT(*) AS n FROM orders",
            accepted=True,
            termination_reason="accepted",
            columns=["n"],
            rows=[[5]],
            row_count=1,
        ),
    )


async def measure(kind: str, repetitions: int = 30) -> dict[str, float | int | str]:
    times: list[float] = []
    model_counts: list[int] = []
    sql_counts: list[int] = []
    tokens: list[int] = []
    for number in range(repetitions):
        completer = MockCompleter(kind)
        sql_calls = 0

        async def sql_handler(request: ChatRequest) -> ChatResponse:
            nonlocal sql_calls
            sql_calls += 1
            return accepted().model_copy(
                update={"session_id": request.session_id, "resolved_question": request.message}
            )

        agent = ConversationalAnalyticsAgent(
            bridge=cast(ConversationalAnalyticsBridge, object()),
            completers={"agentrouter": completer},
            get_state=lambda session_id: None,
            get_last_response=lambda session_id, db_id: cast(RecentResult | None, None),
            sql_handler=sql_handler,
        )
        message = "Hello" if kind == "greeting" else "How many orders are there?"
        started = perf_counter()
        result = await agent.run(
            ChatRequest(
                session_id=f"benchmark_{number}", db_id="benchmark",
                provider="agentrouter", model="mock", message=message,
            )
        )
        times.append((perf_counter() - started) * 1_000)
        model_counts.append(completer.calls)
        sql_counts.append(sql_calls)
        tokens.append(result.token_usage.total_tokens or 0)
    return {
        "case": kind,
        "repetitions": repetitions,
        "median_orchestration_ms": round(median(times), 3),
        "model_calls": model_counts[-1],
        "governed_sql_calls": sql_counts[-1],
        "mock_tokens": tokens[-1],
    }


async def main() -> None:
    for kind in ("greeting", "new_query"):
        print(json.dumps(await measure(kind), sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
