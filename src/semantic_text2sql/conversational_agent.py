"""Bounded, opt-in conversational agent around the governed Text-to-SQL path.

The agent chooses capabilities; it never receives a database connection or
permission to execute model-authored SQL. Analytical work delegates to chat.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from statistics import mean, median
from time import perf_counter
from typing import Any, Literal, cast, get_args
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from pydantic import Field, ValidationError
from sqlglot import exp, parse_one

from semantic_text2sql.conversational_bridge import (
    ConversationalAnalyticsBridge,
    MetadataToolInput,
)
from semantic_text2sql.database import DatabaseError
from semantic_text2sql.llm import ModelError, StructuredOutputUnsupported
from semantic_text2sql.models import (
    CallLedgerEntry,
    ChatRequest,
    ChatResponse,
    ConversationState,
    StrictModel,
    TokenUsage,
)
from semantic_text2sql.result_cache import RecentResult
from semantic_text2sql.runtime import RequestBudget, RequestBudgetExceeded, use_request_budget

ToolName = Literal[
    "list_tables",
    "search_schema",
    "describe_table",
    "get_relationships",
    "get_column_profile",
    "get_available_metrics",
    "get_business_context",
    "ask_data",
    "analyze_result",
    "check_sql_correctness",
    "change_visualization",
    "present_result",
    "explain_existing_result",
    "reset_context",
]


class ToolInput(StrictModel):
    """The only model-controlled tool arguments; never executable SQL."""

    objective: str = Field(min_length=1, max_length=4_000)


class AgentDecision(StrictModel):
    action: Literal["ANSWER", "CALL_TOOL"]
    intent: Literal[
        "DIRECT_ANSWER",
        "NEW_QUERY",
        "MODIFY_QUERY",
        "VISUALIZE",
        "EXPLORE_SCHEMA",
        "BUSINESS_CONTEXT",
        "EXPLAIN",
        "INVESTIGATE",
        "CORRECT",
        "REVIEW_SQL",
        "OPTIMIZE",
        "RESET_CONTEXT",
    ]
    tool: ToolName | None = None
    tool_arguments: ToolInput | None = None
    instruction: str | None = Field(default=None, max_length=4_000)
    answer: str | None = Field(default=None, max_length=2_000)


_ACTION_VALUES: tuple[str, ...] = tuple(get_args(AgentDecision.model_fields["action"].annotation))
_INTENT_VALUES: tuple[str, ...] = tuple(get_args(AgentDecision.model_fields["intent"].annotation))
_TOOL_VALUES: tuple[str, ...] = tuple(get_args(ToolName))


_DECISION_EXAMPLES: tuple[dict[str, Any], ...] = (
    {
        "action": "CALL_TOOL",
        "intent": "NEW_QUERY",
        "tool": "ask_data",
        "tool_arguments": {"objective": "Show total consumption by month"},
        "instruction": None,
        "answer": None,
    },
    {
        "action": "CALL_TOOL",
        "intent": "VISUALIZE",
        "tool": "change_visualization",
        "tool_arguments": {"objective": "Plot the accepted monthly result"},
        "instruction": None,
        "answer": None,
    },
    {
        "action": "ANSWER",
        "intent": "DIRECT_ANSWER",
        "tool": None,
        "tool_arguments": None,
        "instruction": None,
        "answer": "Hello. I can help analyze your database.",
    },
)


_TOOL_INPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["objective"],
    "properties": {"objective": {"type": "string", "minLength": 1, "maxLength": 4_000}},
}


AGENT_DECISION_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "intent", "tool", "tool_arguments", "instruction", "answer"],
    "properties": {
        "action": {"type": "string", "enum": list(_ACTION_VALUES)},
        "intent": {"type": "string", "enum": list(_INTENT_VALUES)},
        "tool": {
            "anyOf": [
                {"type": "string", "enum": list(_TOOL_VALUES)},
                {"type": "null"},
            ]
        },
        "tool_arguments": {"anyOf": [_TOOL_INPUT_SCHEMA, {"type": "null"}]},
        "instruction": {"type": ["string", "null"], "maxLength": 4_000},
        "answer": {"type": ["string", "null"], "maxLength": 2_000},
    },
}


def _decision_json_line(example: dict[str, Any]) -> str:
    return json.dumps(example, separators=(",", ":"))


_DECISION_INSTRUCTIONS = (
    "Decision schema rules: Return ONLY one structured AgentDecision JSON object with keys "
    "action, intent, tool, tool_arguments, answer; no prose, markdown, or code fences. "
    "action must be exactly one of: "
    + ", ".join(_ACTION_VALUES)
    + ". intent must be exactly one of: "
    + ", ".join(_INTENT_VALUES)
    + "; it is a machine identifier, never a natural-language sentence. "
    "tool must be exactly one of: "
    + ", ".join(_TOOL_VALUES)
    + " or null. "
    'For CALL_TOOL, tool_arguments is {"objective": "a short, scoped natural-language '
    'objective"}; otherwise use null. The legacy instruction key is accepted '
    "for compatibility, but prefer tool_arguments. Complete JSON schema:\n"
    + json.dumps(AGENT_DECISION_SCHEMA, separators=(",", ":"))
    + "\nExamples:\n"
    + "\n".join(_decision_json_line(example) for example in _DECISION_EXAMPLES)
)


def _extract_decision_object(value: str) -> str:
    """Require the complete response to be JSON; never salvage embedded prose."""
    return str(value).strip()


def _validation_issue_lines(exc: ValidationError) -> list[str]:
    """Render bounded, non-exception validation issues for model repair prompts."""
    lines: list[str] = []
    for error in exc.errors()[:4]:
        location = ".".join(str(part) for part in error["loc"]) or "decision"
        if error["type"] == "literal_error":
            ctx = error.get("ctx") or {}
            expected = error.get("expected") or ctx.get("expected")
            lines.append(
                f"{location} must be one of {expected}"
                if isinstance(expected, str)
                else f"{location} used an invalid literal value"
            )
        elif error["type"] == "missing":
            lines.append(f"{location} is required")
        elif error["type"] in {"json_invalid", "invalid_json"}:
            lines.append("response is not a valid JSON object")
        else:
            lines.append(f"{location} {error['msg']}")
    if not lines:
        lines.append("response could not be parsed as a valid AgentDecision")
    return lines


def _consistency_issue_lines(decision: AgentDecision) -> list[str]:
    issues: list[str] = []
    if decision.action == "CALL_TOOL" and decision.tool is None:
        issues.append(
            "tool is required when action is CALL_TOOL; choose one of "
            + ", ".join(_TOOL_VALUES)
        )
    if decision.action == "ANSWER" and decision.tool is not None:
        issues.append(
            f"tool must be null when action is {decision.action}; observed {decision.tool}"
        )
    if decision.action == "CALL_TOOL" and not (
        decision.tool_arguments or (decision.instruction and decision.instruction.strip())
    ):
        issues.append("CALL_TOOL requires tool_arguments.objective or instruction")
    if decision.action != "CALL_TOOL" and decision.tool_arguments is not None:
        issues.append("tool_arguments must be null unless action is CALL_TOOL")
    if decision.action == "ANSWER" and not (decision.answer or "").strip():
        issues.append(f"{decision.action} requires a nonempty answer")
    intent_tools = {
        "NEW_QUERY": {"ask_data"},
        "MODIFY_QUERY": {"ask_data"},
        "VISUALIZE": {"change_visualization", "present_result"},
        "EXPLORE_SCHEMA": {"list_tables", "search_schema", "get_column_profile"},
        "BUSINESS_CONTEXT": {"get_available_metrics", "get_business_context"},
        "EXPLAIN": {"explain_existing_result", "present_result"},
        "INVESTIGATE": {"analyze_result", "ask_data"},
        "CORRECT": {"check_sql_correctness", "ask_data"},
        "REVIEW_SQL": {"check_sql_correctness"},
        "OPTIMIZE": {"ask_data"},
        "RESET_CONTEXT": {"reset_context"},
    }
    if (
        decision.action == "CALL_TOOL"
        and decision.intent in intent_tools
        and decision.tool not in intent_tools[decision.intent]
    ):
        issues.append(f"tool {decision.tool} is incompatible with intent {decision.intent}")
    return issues


class AgentState(StrictModel):
    observations: list[dict[str, Any]] = Field(default_factory=list, max_length=4)
    decision: AgentDecision | None = None
    response: ChatResponse | None = None
    base_response: ChatResponse | None = None
    steps: int = 0
    usage: TokenUsage = Field(default_factory=TokenUsage)
    ledger: list[CallLedgerEntry] = Field(default_factory=list)


class ToolObservation(StrictModel):
    tool: ToolName
    facts: dict[str, Any]
    evidence_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class _DecisionCompletion:
    """Outcome of one decision-fetch round; failures keep usage and ledger entries."""

    decision: AgentDecision | None
    usage: TokenUsage
    ledger: list[CallLedgerEntry]
    issues: list[str]
    message: str | None = None


CACHED_RESULT_TOOLS: frozenset[ToolName] = frozenset({"analyze_result", "present_result"})
PRIOR_SQL_TOOLS: frozenset[ToolName] = frozenset(
    {"change_visualization", "check_sql_correctness"}
)


class ConversationalAnalyticsAgent:
    """One LangGraph reason/act loop with explicit tool and model budgets."""

    def __init__(
        self,
        *,
        bridge: ConversationalAnalyticsBridge,
        completers: dict[str, Any],
        get_state: Callable[[str], ConversationState | None],
        get_last_response: Callable[[str, str], RecentResult | ChatResponse | None],
        sql_handler: Callable[[ChatRequest], Awaitable[ChatResponse]],
        investigation_handler: Callable[[ChatRequest], Awaitable[ChatResponse]] | None = None,
    ) -> None:
        self.bridge = bridge
        self.completers = completers
        self.get_state = get_state
        self.get_last_response = get_last_response
        self.sql_handler = sql_handler
        self.investigation_handler = investigation_handler
        graph = StateGraph(AgentState)
        graph.add_node("reason", self._reason)
        graph.add_node("act", self._act)
        graph.add_edge(START, "reason")
        graph.add_conditional_edges("reason", self._next, {"act": "act", "end": END})
        graph.add_conditional_edges("act", self._after_act, {"act": "reason", "end": END})
        self.graph = graph.compile()
        self._request: ChatRequest | None = None
        self._budget: RequestBudget | None = None
        self._investigation_id: str | None = None

    async def run(self, request: ChatRequest) -> ChatResponse:
        # The compiled graph is immutable, but request-local data must not be
        # stored on a shared agent instance. Create a request-local runner.
        runner = self._for_request(request)
        assert runner._budget is not None
        with use_request_budget(runner._budget):
            return await runner._run_request(request)

    async def _run_request(self, request: ChatRequest) -> ChatResponse:
        try:
            result = await self.graph.ainvoke(AgentState())
            state = AgentState.model_validate(result)
            if state.response is not None:
                return state.response
            return self._failure("Investigation limit reached without enough evidence.")
        except ModelError as exc:
            return self._failure(self._provider_failure_message(exc))
        except (DatabaseError, RequestBudgetExceeded, ValueError) as exc:
            return self._failure(f"The assistant could not complete this safely: {exc}")
        except ValidationError:
            return self._failure(
                "I couldn't interpret that request safely. Please try rephrasing it."
            )
        except Exception as exc:  # Provider adapters can raise transport-specific errors.
            return self._failure(f"Assistant response unavailable ({type(exc).__name__}).")

    @staticmethod
    def _provider_failure_message(error: Exception) -> str:
        """Give actionable, non-sensitive provider diagnostics to the client."""
        detail = str(error).lower()
        if "not configured" in detail or ("api key" in detail and "missing" in detail):
            cause = "The selected model's API configuration is missing."
        elif any(
            term in detail for term in ("rate limit", "rate_limit", "too many requests", "429")
        ):
            cause = "The model provider is rate-limiting requests. Please try again shortly."
        elif any(term in detail for term in ("insufficient credits", "quota exceeded", "billing")):
            cause = "The model provider's quota or credits are unavailable."
        elif any(
            term in detail
            for term in ("unauthorized", "forbidden", "invalid api key", "401", "403")
        ):
            cause = "The model provider rejected the configured credentials or access."
        elif isinstance(error, StructuredOutputUnsupported):
            cause = "The selected model does not support the required decision format."
        elif any(term in detail for term in ("timeout", "timed out")):
            cause = "The model provider did not respond before the timeout."
        elif "no text output" in detail or "no structured decision" in detail:
            cause = "The model provider returned an empty decision."
        else:
            cause = "The model provider rejected or failed the decision request."
        return cause + " No query was run; please retry or select another configured model."

    @staticmethod
    def _capabilities() -> list[str]:
        return [
            "answer analytical questions through the governed Text-to-SQL pipeline",
            "explain accepted results and SQL",
            "inspect schema and approved business definitions",
            "make validated charts from complete accepted results",
        ]

    def _failure(self, message: str) -> ChatResponse:
        return self._plain(message, provenance=["assistant_service_failure"])

    def _for_request(self, request: ChatRequest) -> ConversationalAnalyticsAgent:
        runner = object.__new__(ConversationalAnalyticsAgent)
        runner.__dict__ = self.__dict__.copy()
        runner._request = request
        runner._investigation_id = uuid4().hex
        runner._budget = RequestBudget(
            timeout_seconds=float(os.environ.get("TEXT2SQL_ASSISTANT_TIMEOUT_SECONDS", "180")),
            max_model_calls=int(os.environ.get("TEXT2SQL_ASSISTANT_MAX_MODEL_CALLS", "8")),
            max_database_calls=int(os.environ.get("TEXT2SQL_ASSISTANT_MAX_DATABASE_CALLS", "24")),
        )
        # Bind graph nodes to this request-local instance.
        graph = StateGraph(AgentState)
        graph.add_node("reason", runner._reason)
        graph.add_node("act", runner._act)
        graph.add_edge(START, "reason")
        graph.add_conditional_edges("reason", runner._next, {"act": "act", "end": END})
        graph.add_conditional_edges("act", runner._after_act, {"act": "reason", "end": END})
        runner.graph = graph.compile()
        return runner

    def _plain(self, message: str, *, provenance: list[str] | None = None) -> ChatResponse:
        assert self._request is not None
        return ChatResponse(
            session_id=self._request.session_id,
            operation="EXPLAIN_CONTEXT",
            resolved_question=self._request.message,
            message=message,
            provenance=provenance or [],
        )

    @staticmethod
    def _sum_usage(left: TokenUsage, right: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=(left.input_tokens or 0) + (right.input_tokens or 0),
            output_tokens=(left.output_tokens or 0) + (right.output_tokens or 0),
            cache_read_tokens=(left.cache_read_tokens or 0) + (right.cache_read_tokens or 0),
            cache_creation_tokens=(left.cache_creation_tokens or 0)
            + (right.cache_creation_tokens or 0),
        )

    @staticmethod
    def _model_entry(
        provider: str, model: str, usage: TokenUsage, *, succeeded: bool
    ) -> CallLedgerEntry:
        """Ledger entry for one top-layer model call, valid output or not."""
        return CallLedgerEntry(
            component="conversational_agent",
            kind="MODEL",
            status="SUCCEEDED" if succeeded else "FAILED",
            provider=provider,
            requested_model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )

    async def _complete_decision(
        self,
        prompt: str,
        state: AgentState,
        provider: str,
        model: str,
        completer: Any,
    ) -> _DecisionCompletion:
        """Fetch and validate one AgentDecision with at most one bounded repair.

        Provider-native JSON-schema output is used when the completer exposes it;
        local validation always applies. Invalid output never executes a tool and
        never converts prose into intent: the exact issues are fed back once, and
        a still-invalid response fails closed. Usage and ledger survive failures.
        """
        assert self._budget is not None
        structured = getattr(completer, "complete_structured", None)
        usage_total = TokenUsage()
        ledger: list[CallLedgerEntry] = []
        issues: list[str] = []
        native_supported = callable(structured)
        for attempt in range(2):
            request_prompt = (
                prompt if attempt == 0 or not issues else prompt + self._repair_tail(issues)
            )
            if native_supported:
                call = cast(
                    Any,
                    cast(Any, structured)(
                        model, request_prompt, "AgentDecision", AGENT_DECISION_SCHEMA
                    ),
                )
            else:
                call = completer.complete_detailed(model, request_prompt)
            try:
                raw, usage = await self._budget.wait(call, kind="model")
            except StructuredOutputUnsupported:
                ledger.append(self._model_entry(provider, model, TokenUsage(), succeeded=False))
                native_supported = False
                try:
                    raw, usage = await self._budget.wait(
                        completer.complete_detailed(model, request_prompt), kind="model"
                    )
                except Exception as exc:
                    ledger.append(self._model_entry(provider, model, TokenUsage(), succeeded=False))
                    return _DecisionCompletion(
                        None,
                        usage_total,
                        ledger,
                        ["structured output unavailable"],
                        message=self._provider_failure_message(exc),
                    )
            except RequestBudgetExceeded:
                ledger.append(self._model_entry(provider, model, TokenUsage(), succeeded=False))
                return _DecisionCompletion(
                    None,
                    usage_total,
                    ledger,
                    ["the assistant call budget was exhausted before a valid decision"],
                    message=(
                        "The assistant could not complete this safely: the assistant call "
                        "budget was exhausted before a valid decision was produced."
                    ),
                )
            except Exception as exc:
                ledger.append(self._model_entry(provider, model, TokenUsage(), succeeded=False))
                transport_message = self._provider_failure_message(exc)
                issues = [
                    *issues,
                    f"then the repair call failed ({type(exc).__name__})",
                ]
                return _DecisionCompletion(
                    None,
                    usage_total,
                    ledger,
                    issues,
                    message=transport_message,
                )
            usage_total = self._sum_usage(usage_total, usage)
            try:
                decision = AgentDecision.model_validate_json(
                    _extract_decision_object(str(raw))
                )
            except ValidationError as exc:
                issues = _validation_issue_lines(exc)
                ledger.append(self._model_entry(provider, model, usage, succeeded=False))
                continue
            issues = _consistency_issue_lines(decision)
            if issues:
                ledger.append(self._model_entry(provider, model, usage, succeeded=False))
                continue
            ledger.append(self._model_entry(provider, model, usage, succeeded=True))
            return _DecisionCompletion(decision, usage_total, ledger, [])
        return _DecisionCompletion(None, usage_total, ledger, issues)

    @staticmethod
    def _repair_tail(issues: list[str]) -> str:
        return (
            "\n\nThe previous response was not a valid AgentDecision. Return ONLY one "
            "corrected JSON object that matches the AgentDecision schema: no prose, "
            "markdown, or code fences.\nValidation issues:\n- "
            + "\n- ".join(issues)
            + "\nAllowed values: action="
            + "|".join(_ACTION_VALUES)
            + "; intent="
            + "|".join(_INTENT_VALUES)
            + "; tool="
            + "|".join(_TOOL_VALUES)
            + "|null.\n"
            + "Consistency: action CALL_TOOL requires a tool; ANSWER requires "
            "tool null.\nExamples:\n"
            + "\n".join(_decision_json_line(example) for example in _DECISION_EXAMPLES)
        )

    @staticmethod
    def _decision_failure_message(issues: list[str]) -> str:
        return (
            "I couldn't interpret that request safely. Please try rephrasing it. "
            "No tool was run and your previous result was preserved."
        )

    @staticmethod
    def _result_facts(response: ChatResponse) -> dict[str, Any]:
        generation = response.generation
        return {
            "kind": "governed_sql_result",
            "status": "executed" if generation and generation.accepted else "not_accepted",
            "run_id": response.run_id,
            "question": response.resolved_question,
            "columns": generation.columns if generation else [],
            "rows_preview": generation.rows[:10] if generation else [],
            "row_count": generation.row_count if generation else 0,
            "truncated": generation.truncated if generation else False,
            "chart": (
                response.presentation.chart_spec.model_dump(exclude={"data"})
                if response.presentation and response.presentation.chart_spec
                else None
            ),
            "failure": response.message if not generation or not generation.accepted else None,
        }

    @staticmethod
    def _next(state: AgentState) -> Literal["act", "end"]:
        return (
            "act"
            if state.decision and state.decision.action == "CALL_TOOL" and state.response is None
            else "end"
        )

    @staticmethod
    def _after_act(state: AgentState) -> Literal["act", "end"]:
        return "end" if state.response is not None else "act"

    async def _reason(self, state: AgentState) -> AgentState:
        assert self._request is not None and self._budget is not None
        previous = self.get_state(self._request.session_id)
        if previous and previous.db_id != self._request.db_id:
            previous = None
        last = self.get_last_response(self._request.session_id, self._request.db_id)
        compact = {
            "question": self._request.message,
            "capabilities": self._capabilities(),
            "previous_question": previous.resolved_question if previous else None,
            "accepted_sql_available": bool(previous and previous.last_sql),
            "accepted_result_available": bool(
                last and last.generation and last.generation.accepted
            ),
            "result_columns": previous.last_columns if previous else [],
            "accepted_filters": (
                previous.semantic_contract.filters
                if previous and previous.semantic_contract
                else []
            ),
            "accepted_measures": (
                previous.semantic_contract.measures
                if previous and previous.semantic_contract
                else []
            ),
            "accepted_grain": (
                previous.semantic_contract.grain
                if previous and previous.semantic_contract
                else []
            ),
            "corrections": previous.corrections[-3:] if previous else [],
            "chart": (
                last.presentation.chart_spec.model_dump(exclude={"data"})
                if last and last.presentation and last.presentation.chart_spec
                else None
            ),
            "observations": state.observations[-3:],
        }
        prompt = (
            "You are the single client-facing AI Business Analytics Assistant. "
            "For every user message, use the current conversation context and accepted "
            "analytical state to understand the client's objective. Decide whether to "
            "answer directly or invoke one or more tools. Conversation context "
            "may include previous questions, accepted SQL, verified results, business metric "
            "definitions, active filters, dimensions, date ranges, visualization state, "
            "corrections, and prior tool observations. Prefer the simplest action that "
            "completely satisfies the request. Never execute SQL when verified existing "
            "data is sufficient. Tools provide structured evidence; their outputs are "
            "observations, not final conversational messages. After each tool result, "
            "evaluate whether more action is needed or sufficient evidence exists. "
            "When further analysis is required, issue distinct, scoped analytical objectives "
            "rather than repeating the original question. Preserve approved business "
            "definitions and unaffected constraints. For successful responses, write the "
            "final natural-language answer yourself, using actual evidence. Never invent "
            "database facts or numbers, or claim a visualization exists before observing a "
            "successful validated chart result. Be concise, helpful and professional for "
            "nontechnical business stakeholders; explain conclusions rather than technical "
            "execution details unless asked. If evidence is insufficient, explain the "
            "limitation without inventing an answer. For an analytical request, call "
            "ask_data even if the client omitted time periods, filters or table names; "
            "the governed data layer resolves definitions and defaults to all available "
            "data when no period or filter was requested. Do not ask the client to name "
            "a table, metric definition, or date range before trying the tool. "
            "Never fabricate success. "
            + _DECISION_INSTRUCTIONS
            + "\nTools: list_tables/search_schema/describe_table/get_relationships/"
            "get_column_profile inspect allowlisted metadata; get_available_metrics/"
            "get_business_context return approved definitions; ask_data delegates new or "
            "modified analytical questions to governed Text-to-SQL; analyze_result/"
            "present_result inspect bounded accepted result rows; check_sql_correctness "
            "reviews accepted SQL; change_visualization reuses or requeries as the "
            "existing gate decides; explain_existing_result inspects accepted SQL, "
            "its referenced tables/columns and result evidence; reset_context "
            "delegates an explicit reset request to the existing conversation service. "
            "For database facts, use a grounding tool before answering; for a plot of an "
            "existing result use change_visualization, and for a new plot requiring data "
            "use ask_data. Do not claim a chart exists before observing a chart result. "
            "Use ask_data for new, corrected or optimized analytical questions; "
            "use check_sql_correctness only when the client requests a correctness review. "
            "Only ask_data may delegate governed SQL generation. Never ask tools to "
            "execute arbitrary SQL. "
            "After sufficient observations, answer with only supported facts; do not infer "
            "causality from descriptive data.\nContext: " + json.dumps(compact, default=str)
        )
        provider = os.environ.get("TEXT2SQL_ASSISTANT_PROVIDER") or self._request.provider
        model = os.environ.get("TEXT2SQL_ASSISTANT_MODEL") or self._request.model
        if provider not in self.completers:
            raise ValueError(f"Assistant provider {provider} is not configured.")
        completer = self.completers[provider]
        completion = await self._complete_decision(prompt, state, provider, model, completer)
        if completion.decision is None:
            failed_ledger = [*state.ledger, *completion.ledger]
            merged_failed_usage = self._sum_usage(state.usage, completion.usage)
            if state.base_response is not None:
                # SQL may already have executed and updated the conversation.
                # Preserve that governed result if final prose generation fails.
                return state.model_copy(
                    update={
                        "ledger": failed_ledger,
                        "response": state.base_response.model_copy(
                            update={
                                "token_usage": merged_failed_usage,
                                "call_ledger": failed_ledger,
                                "provenance": [
                                    *state.base_response.provenance,
                                    "assistant_response_unavailable",
                                ],
                            }
                        ),
                    }
                )
            failure = self._plain(
                (
                    completion.message
                    if completion.message is not None
                    else self._decision_failure_message(completion.issues)
                ),
                provenance=["assistant_service_failure"],
            )
            return state.model_copy(
                update={
                    "ledger": failed_ledger,
                    "response": failure.model_copy(
                        update={
                            "token_usage": merged_failed_usage,
                            "call_ledger": failed_ledger,
                        }
                    ),
                }
            )
        decision = completion.decision
        if not state.observations and decision.action == "ANSWER":
            required_tool: ToolName | None = {
                "NEW_QUERY": "ask_data",
                "MODIFY_QUERY": "ask_data",
                "INVESTIGATE": "ask_data",
                "CORRECT": "ask_data",
                "REVIEW_SQL": "check_sql_correctness",
                "OPTIMIZE": "ask_data",
                "RESET_CONTEXT": "reset_context",
                "VISUALIZE": "change_visualization" if last else "ask_data",
                "EXPLORE_SCHEMA": "search_schema",
                "BUSINESS_CONTEXT": "get_business_context",
                "EXPLAIN": "explain_existing_result",
            }.get(decision.intent)
            if required_tool is not None:
                # The top model selects intent, but cannot assert database facts,
                # accepted SQL details or a chart before observing the tool result.
                decision = decision.model_copy(
                    update={"action": "CALL_TOOL", "tool": required_tool, "answer": None}
                )
        merged_usage = self._sum_usage(state.usage, completion.usage)
        ledger = [*state.ledger, *completion.ledger]
        if decision.action != "CALL_TOOL":
            answer = decision.answer or "I could not complete that request safely."
            base = state.base_response or self._plain(answer)
            evidence_runs = [
                str(item.get("run_id") or item["evidence_id"])
                for item in state.observations
                if item.get("run_id") or item.get("evidence_id")
            ]
            presentation = base.presentation
            if presentation is not None:
                presentation = presentation.model_copy(
                    update={"summary": answer[:1800], "source": "model"}
                )
            return state.model_copy(
                update={
                    "decision": decision,
                    "usage": merged_usage,
                    "ledger": ledger,
                    "response": base.model_copy(
                        update={
                            "message": answer,
                            "explanation": answer if base.explanation is not None else None,
                            "presentation": presentation,
                            "provenance": [*base.provenance, *evidence_runs],
                            "clarification_required": base.clarification_required,
                            "clarification_question": base.clarification_question,
                            "token_usage": self._sum_usage(base.token_usage, merged_usage),
                            "call_ledger": [*base.call_ledger, *ledger],
                        }
                    ),
                }
            )
        if not decision.tool or state.steps >= 3:
            return state.model_copy(
                update={
                    "response": self._failure(
                        "The investigation limit was reached without enough evidence."
                    ),
                    "usage": merged_usage,
                    "ledger": ledger,
                }
            )
        return state.model_copy(
            update={"decision": decision, "usage": merged_usage, "ledger": ledger}
        )

    async def _act(self, state: AgentState) -> AgentState:
        assert self._request is not None and self._budget is not None and state.decision is not None
        tool = state.decision.tool
        if tool is None:
            raise ValueError("The agent did not select a tool.")
        tool_input = state.decision.tool_arguments or ToolInput(
            objective=state.decision.instruction or self._request.message
        )
        question = tool_input.objective
        started = perf_counter()
        facts: dict[str, Any]
        last = self.get_last_response(self._request.session_id, self._request.db_id)
        previous = self.get_state(self._request.session_id)
        has_prior_sql = bool(
            previous and previous.db_id == self._request.db_id and previous.last_sql
        )
        if (tool in CACHED_RESULT_TOOLS and last is None) or (
            tool in PRIOR_SQL_TOOLS and not has_prior_sql
        ):
            facts = {"tool": tool, "error": "No accepted result is available."}
            return state.model_copy(
                update={
                    "observations": [*state.observations, facts],
                    "decision": None,
                    "steps": state.steps + 1,
                    "ledger": [
                        *state.ledger,
                        CallLedgerEntry(
                            component=f"assistant_tool:{tool}",
                            kind="TOOL",
                            status="FAILED",
                            latency_ms=round((perf_counter() - started) * 1000),
                        ),
                    ],
                }
            )
        base = state.base_response
        downstream_usage = TokenUsage()
        downstream_ledger: list[CallLedgerEntry] = []
        if tool in {"ask_data", "check_sql_correctness", "reset_context"}:
            if tool == "check_sql_correctness":
                request_message = "Check correctness of the previous SQL. " + self._request.message
            elif state.decision.intent == "INVESTIGATE":
                # An investigation must be able to ask genuinely different
                # governed analytical questions. The original request remains
                # in evidence; the SQL engine still owns validation/execution.
                request_message = question
                if request_message.strip().casefold() == self._request.message.strip().casefold():
                    facts = {
                        "tool": tool,
                        "error": (
                            "Investigation objective repeats the original question; "
                            "request a distinct analytical breakdown."
                        ),
                    }
                    return state.model_copy(update={
                        "observations": [*state.observations, facts],
                        "decision": None,
                        "steps": state.steps + 1,
                    })
            else:
                request_message = self._request.message
            investigation_evidence = self._request.evidence
            if state.decision.intent == "INVESTIGATE":
                accepted = previous if previous and previous.db_id == self._request.db_id else None
                constraints = (
                    accepted.semantic_contract.model_dump(
                        mode="json", exclude_none=True, exclude_defaults=True
                    )
                    if accepted and accepted.semantic_contract else {}
                )
                investigation_evidence = json.dumps({
                    "original_client_request": self._request.message,
                    "previous_accepted_question": accepted.resolved_question if accepted else None,
                    "approved_analytical_constraints": constraints,
                    "trusted_evidence": self._request.evidence,
                }, separators=(",", ":"))
            request = self._request.model_copy(
                update={
                    "message": request_message,
                    "session_id": (
                        f"inv_{self._investigation_id}_{state.steps}"
                        if state.decision.intent == "INVESTIGATE"
                        else self._request.session_id
                    ),
                    "parent_run_id": (
                        None
                        if state.decision.intent == "INVESTIGATE"
                        else self._request.parent_run_id
                    ),
                    "evidence": investigation_evidence,
                }
            )
            handler = (
                (self.investigation_handler or self.sql_handler)
                if state.decision.intent == "INVESTIGATE"
                else self.sql_handler
            )
            response = await handler(request)
            facts = {"tool": tool, **self._result_facts(response)}
            if state.decision.intent == "INVESTIGATE" and response.generation:
                facts["evidence_id"] = request.session_id
                facts["calculated_evidence"] = self._analyze_result(response)
            downstream_usage = response.token_usage
            downstream_ledger = response.call_ledger
            if state.decision.intent != "INVESTIGATE":
                # Account each delegated call once, including when the model
                # invokes more than one tool in the same conversation turn.
                base = response.model_copy(
                    update={"token_usage": TokenUsage(), "call_ledger": []}
                )
        elif tool in {"get_business_context", "get_available_metrics"}:
            output = self.bridge.get_business_context(
                MetadataToolInput(
                    db_id=self._request.db_id,
                    question=question
                    if tool == "get_business_context"
                    else "What metrics are available?",
                )
            )
            facts = ToolObservation(
                tool=tool, facts={"definitions": output.facts, "found": output.found},
                evidence_id=output.evidence_id,
            ).model_dump()
        elif tool in {
            "list_tables",
            "search_schema",
            "describe_table",
            "get_relationships",
            "get_column_profile",
        }:
            self._budget.claim("database")
            output = await self.bridge.inspect_metadata(
                self._request.db_id,
                cast(
                    Literal[
                        "list_tables",
                        "search_schema",
                        "describe_table",
                        "get_relationships",
                        "get_column_profile",
                    ],
                    tool,
                ),
                question,
            )
            facts = ToolObservation(
                tool=tool, facts={"metadata": output.facts, "found": output.found},
                evidence_id=output.evidence_id,
            ).model_dump()
        elif tool == "analyze_result":
            last = self.get_last_response(self._request.session_id, self._request.db_id)
            if last is None:
                facts = {"tool": tool, "error": "No accepted result is available."}
            else:
                facts = {"tool": tool, **self._analyze_result(last)}
        elif tool == "explain_existing_result":
            last = self.get_last_response(self._request.session_id, self._request.db_id)
            previous = self.get_state(self._request.session_id)
            sql = last.generation.sql if last and last.generation else None
            if not sql and previous and previous.db_id == self._request.db_id:
                sql = previous.last_sql
            references = None
            if sql:
                tree = parse_one(sql)
                references = {
                    "tables": sorted({node.name for node in tree.find_all(exp.Table)}),
                    "columns": sorted({node.sql() for node in tree.find_all(exp.Column)}),
                }
            facts = (
                {
                    "tool": tool,
                    "result": self._cached_facts(last, include_sql=True) if last else None,
                    "accepted_sql_references": references,
                }
                if sql or last
                else {"tool": tool, "error": "No accepted SQL or result is available."}
            )
        elif tool == "present_result":
            last = self.get_last_response(self._request.session_id, self._request.db_id)
            facts = {"tool": tool, "result": self._cached_facts(last) if last else None}
            if last and last.generation:
                base = ChatResponse(
                    session_id=self._request.session_id,
                    operation="EXPLAIN_RESULT",
                    resolved_question=last.generation.question,
                    generation=last.generation,
                    presentation=last.presentation,
                    message=last.message,
                )
        elif tool == "change_visualization":
            # The established visualization path is authoritative for the
            # reusability gate, chart validity, and whether fresh SQL is needed.
            prior_result = self.get_last_response(self._request.session_id, self._request.db_id)
            message = self._request.message
            if tool == "change_visualization" and message.strip().casefold() in {
                "plot", "chart", "graph", "visualize", "visualise",
            }:
                # The top model chose a visualization of the accepted result;
                # make a bare command referential for the existing reuse gate.
                message = f"{message.strip()} it"
            request = self._request.model_copy(update={"message": message})
            response = await self.sql_handler(request)
            facts = {"tool": tool, **self._result_facts(response)}
            downstream_usage = response.token_usage
            downstream_ledger = response.call_ledger
            base = response.model_copy(
                update={"token_usage": TokenUsage(), "call_ledger": []}
            )
            if (
                tool == "change_visualization"
                and prior_result and prior_result.generation and response.generation
                and response.operation == "VISUALIZE"
                and response.generation.sql == prior_result.generation.sql
                and not any(item.kind == "MODEL" for item in response.call_ledger)
            ):
                downstream_ledger.append(
                    CallLedgerEntry(
                        component="result_cache_reuse", kind="TOOL", status="SUCCEEDED"
                    )
                )
        else:
            facts = {"tool": tool, "error": "Unsupported tool."}
        return state.model_copy(
            update={
                "observations": [*state.observations, facts],
                "base_response": base,
                "decision": None,
                "steps": state.steps + 1,
                "usage": self._sum_usage(state.usage, downstream_usage),
                "ledger": [
                    *state.ledger,
                    *downstream_ledger,
                    CallLedgerEntry(
                        component=f"assistant_tool:{tool}",
                        kind="TOOL",
                        status="SUCCEEDED" if "error" not in facts else "FAILED",
                        latency_ms=round((perf_counter() - started) * 1000),
                    ),
                ],
            }
        )

    @staticmethod
    def _cached_facts(
        last: RecentResult | ChatResponse, *, include_sql: bool = False
    ) -> dict[str, Any]:
        generation = last.generation
        facts = {
            "columns": generation.columns if generation else [],
            "rows_preview": generation.rows[:10] if generation else [],
            "row_count": generation.row_count if generation else 0,
            "truncated": generation.truncated if generation else False,
            "chart": (
                last.presentation.chart_spec.model_dump(exclude={"data"})
                if last.presentation and last.presentation.chart_spec
                else None
            ),
        }
        if include_sql:
            facts["accepted_sql"] = generation.sql if generation else None
        return facts

    @staticmethod
    def _analyze_result(last: RecentResult | ChatResponse) -> dict[str, Any]:
        generation = last.generation
        if (
            generation is None
            or generation.truncated
            or generation.row_count != len(generation.rows)
        ):
            return {"error": "A complete accepted result is required for numerical analysis."}
        summary: dict[str, Any] = {"row_count": generation.row_count, "columns": {}}
        numeric_columns: list[tuple[int, str]] = []
        for index, name in enumerate(generation.columns):
            values = [row[index] for row in generation.rows]
            numbers = [
                float(value) for value in values
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            ]
            if not numbers:
                continue
            numeric_columns.append((index, name))
            summary["columns"][name] = {
                "count": len(numbers),
                "missing": len(values) - len(numbers),
                "minimum": min(numbers),
                "maximum": max(numbers),
                "mean": mean(numbers),
                "median": median(numbers),
                "sum": sum(numbers),
            }
        dimension_candidates = [
            (index, name)
            for index, name in enumerate(generation.columns)
            if index not in {item[0] for item in numeric_columns}
        ]
        if len(dimension_candidates) == 1 and numeric_columns:
            dimension_index, dimension_name = dimension_candidates[0]
            rankings: dict[str, Any] = {}
            for numeric_index, numeric_name in numeric_columns[:4]:
                ranked = [
                    (str(row[dimension_index]), float(row[numeric_index]))
                    for row in generation.rows
                    if row[dimension_index] is not None
                    and isinstance(row[numeric_index], (int, float))
                    and not isinstance(row[numeric_index], bool)
                ]
                if not ranked:
                    continue
                highest = max(value for _, value in ranked)
                lowest = min(value for _, value in ranked)
                rankings[numeric_name] = {
                    "dimension": dimension_name,
                    "highest_rows": [item for item in ranked if item[1] == highest],
                    "lowest_rows": [item for item in ranked if item[1] == lowest],
                    "note": "Ranking among returned rows only; no causal interpretation.",
                }
            if rankings:
                summary["rankings"] = rankings
        time_columns = [
            (index, name)
            for index, name in enumerate(generation.columns)
            if any(word in name.casefold() for word in ("date", "month", "year", "period"))
        ]
        if len(time_columns) == 1 and numeric_columns and len(generation.rows) >= 2:
            time_index, time_name = time_columns[0]
            measure_candidates = [item for item in numeric_columns if item[0] != time_index]
            if not measure_candidates:
                return summary
            numeric_index, numeric_name = measure_candidates[0]
            points = [
                (str(row[time_index]), float(row[numeric_index]))
                for row in generation.rows
                if row[time_index] is not None
                and isinstance(row[numeric_index], (int, float))
                and not isinstance(row[numeric_index], bool)
            ]
            if (
                len(points) >= 2
                and len({item[0] for item in points}) == len(points)
                and all(
                    re.fullmatch(r"\d{4}(?:-?\d{2}(?:-?\d{2})?)?", item[0])
                    for item in points
                )
            ):
                points.sort(key=lambda item: item[0])
                first, last_point = points[0], points[-1]
                delta = last_point[1] - first[1]
                summary["observed_trend"] = {
                    "time_column": time_name,
                    "measure_column": numeric_name,
                    "from": first,
                    "to": last_point,
                    "absolute_change": delta,
                    "percent_change": (
                        delta / first[1] * 100 if first[1] != 0 else None
                    ),
                    "decline_observed": delta < 0,
                    "note": "Descriptive comparison only; no causal conclusion.",
                }
        return summary
