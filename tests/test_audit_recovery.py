"""Recovery errors must not trigger speculative invocation/retry paths."""
import asyncio

from semantic_text2sql.llm import ModelError
from semantic_text2sql.models import TokenUsage
from semantic_text2sql.recovery import RecoveryCoordinator, RecoveryTools
from semantic_text2sql.runtime import RequestBudget


def investigate(registry, reasoner):  # type: ignore[no-untyped-def]
    budget = RequestBudget(10)
    tools = RecoveryTools(registry, "shop", registry.inspect("shop"), None, budget=budget)
    trace = asyncio.run(RecoveryCoordinator(tools).ainvestigate(
        question="Check the result", failed_sql="SELECT SUM(amount) FROM orders",
        failure_code="NULL_RESULT", failure_message="Unexpected NULL",
        allowed_tables=["orders"], mode="NULL_RESULT",
        completer=reasoner, provider="groq", model="scripted", budget=budget,
    ))
    return trace, budget


def test_internal_type_error_does_not_invoke_provider_a_second_time(registry) -> None:  # type: ignore[no-untyped-def]
    class Broken:
        calls = 0

        async def complete_detailed(self, *args: object) -> tuple[str, TokenUsage]:
            self.calls += 1
            raise TypeError("Internal provider parsing error")

    model = Broken()
    trace, budget = investigate(registry, model)
    assert model.calls == budget.model_calls == trace.usage.llm_calls == 1
    assert len(trace.call_ledger) == 1
    assert trace.call_ledger[0].status == "FAILED"


def test_failed_model_usage_and_full_latency_survive_fallback(registry) -> None:  # type: ignore[no-untyped-def]
    class Broken:
        async def complete_detailed(self, model: str, prompt: str) -> tuple[str, TokenUsage]:
            await asyncio.sleep(0.025)
            raise ModelError("Malformed response", usage=TokenUsage(
                input_tokens=12, output_tokens=3,
            ))

    trace, budget = investigate(registry, Broken())
    assert budget.model_calls == 1
    assert trace.usage.token_usage.input_tokens == 12
    assert trace.usage.token_usage.output_tokens == 3
    assert trace.usage.latency_ms >= 20
    assert len(trace.call_ledger) == 1
    assert trace.call_ledger[0].status == "FAILED"
    assert trace.call_ledger[0].input_tokens == 12
    assert trace.call_ledger[0].latency_ms is not None
    assert trace.call_ledger[0].latency_ms >= 20


def test_malformed_response_preserves_usage_without_marking_decision_success(registry) -> None:  # type: ignore[no-untyped-def]
    class Malformed:
        async def complete_detailed(self, model: str, prompt: str) -> tuple[str, TokenUsage]:
            return "not json", TokenUsage(
                input_tokens=4, output_tokens=2, cache_read_tokens=3,
                cache_creation_tokens=1, provider_retries=1,
            )

    trace, _ = investigate(registry, Malformed())
    assert trace.usage.token_usage.cache_read_tokens == 3
    assert trace.usage.token_usage.cache_creation_tokens == 1
    assert trace.usage.token_usage.provider_retries == 1
    assert trace.call_ledger[0].status == "FAILED"
    assert trace.call_ledger[0].validation_issues == ["RECOVERY_RESPONSE_INVALID_JSON"]
