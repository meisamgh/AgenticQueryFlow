"""Bounded, opt-in conversational agent around the governed Text-to-SQL path.

The agent chooses capabilities; it never receives a database connection or
permission to execute model-authored SQL. Analytical work delegates to chat.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from pydantic import Field, ValidationError
from sqlglot import exp, parse_one

from semantic_text2sql.conversational_bridge import (
    ConversationalAnalyticsBridge,
    MetadataToolInput,
)
from semantic_text2sql.llm import ModelError
from semantic_text2sql.models import (
    CallLedgerEntry,
    ChatRequest,
    ChatResponse,
    ConversationState,
    StrictModel,
    TokenUsage,
)
from semantic_text2sql.result_cache import RecentResult
from semantic_text2sql.runtime import RequestBudget, RequestBudgetExceeded
from semantic_text2sql.visualization import parse_intent

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
]


class AgentDecision(StrictModel):
    action: Literal["ANSWER", "CLARIFY", "CALL_TOOL"]
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
        "CLARIFY",
    ]
    tool: ToolName | None = None
    instruction: str | None = Field(default=None, max_length=4_000)
    answer: str | None = Field(default=None, max_length=2_000)


class AgentState(StrictModel):
    observations: list[dict[str, Any]] = Field(default_factory=list, max_length=4)
    decision: AgentDecision | None = None
    response: ChatResponse | None = None
    steps: int = 0
    usage: TokenUsage = Field(default_factory=TokenUsage)
    ledger: list[CallLedgerEntry] = Field(default_factory=list)


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
    ) -> None:
        self.bridge = bridge
        self.completers = completers
        self.get_state = get_state
        self.get_last_response = get_last_response
        self.sql_handler = sql_handler
        graph = StateGraph(AgentState)
        graph.add_node("reason", self._reason)
        graph.add_node("act", self._act)
        graph.add_edge(START, "reason")
        graph.add_conditional_edges("reason", self._next, {"act": "act", "end": END})
        graph.add_conditional_edges("act", self._after_act, {"act": "reason", "end": END})
        self.graph = graph.compile()
        self._request: ChatRequest | None = None
        self._budget: RequestBudget | None = None

    async def run(self, request: ChatRequest) -> ChatResponse:
        # The compiled graph is immutable, but request-local data must not be
        # stored on a shared agent instance. Create a request-local runner.
        runner = self._for_request(request)
        previous = runner.get_state(request.session_id)
        last = runner.get_last_response(request.session_id, request.db_id)
        if request.message.strip().casefold() in {
            "hello",
            "hi",
            "thanks",
            "thank you",
            "thanks, that's all",
        }:
            message = (
                "You're welcome."
                if "thank" in request.message.casefold()
                else "Hello! Ask me about your data, an earlier result, or the database schema."
            )
            return runner._plain(message)
        if request.message.strip().casefold() in {"what can you do?", "what can you do"}:
            return runner._plain(
                "I can answer data questions, explain SQL and results, inspect available "
                "schema and approved definitions, and change charts using verified results."
            )
        if (
            previous
            and previous.last_sql
            and request.message.strip().casefold()
            in {
                "which columns did you use?",
                "which columns did you use",
                "what columns did you use?",
                "what columns did you use",
                "which tables did you use?",
                "which tables did you use",
            }
        ):
            tree = parse_one(previous.last_sql)
            wants_tables = "tables" in request.message.casefold()
            names = sorted(
                {
                    node.name if wants_tables else node.sql()
                    for node in tree.find_all(exp.Table if wants_tables else exp.Column)
                }
            )
            return runner._plain(
                ("Tables" if wants_tables else "Columns")
                + " in the accepted SQL: "
                + (", ".join(names) if names else "none explicitly referenced")
            )
        intent = parse_intent(request.message, has_previous=previous is not None)
        if (
            intent.followup
            and intent.explicit_type
            and last
            and last.presentation
            and last.presentation.chart_spec
        ):
            return await runner.sql_handler(request)
        try:
            result = await runner.graph.ainvoke(AgentState())
            state = AgentState.model_validate(result)
            if state.response is not None:
                return state.response
            return runner._plain(
                "I could not complete the request within the investigation limits."
            )
        except (ModelError, RequestBudgetExceeded, ValidationError, ValueError) as exc:
            return runner._plain(f"I could not complete this safely: {exc}")

    def _for_request(self, request: ChatRequest) -> ConversationalAnalyticsAgent:
        runner = object.__new__(ConversationalAnalyticsAgent)
        runner.__dict__ = self.__dict__.copy()
        runner._request = request
        runner._budget = RequestBudget(
            timeout_seconds=float(os.environ.get("TEXT2SQL_ASSISTANT_TIMEOUT_SECONDS", "60")),
            max_model_calls=int(os.environ.get("TEXT2SQL_ASSISTANT_MAX_MODEL_CALLS", "4")),
            max_database_calls=int(os.environ.get("TEXT2SQL_ASSISTANT_MAX_DATABASE_CALLS", "4")),
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
        last = self.get_last_response(self._request.session_id, self._request.db_id)
        compact = {
            "question": self._request.message,
            "previous_question": previous.resolved_question if previous else None,
            "accepted_sql": previous.last_sql if previous else None,
            "result_columns": previous.last_columns if previous else [],
            "accepted_filters": (
                previous.semantic_contract.filters
                if previous and previous.semantic_contract
                else []
            ),
            "chart": (
                last.presentation.chart_spec.model_dump(exclude={"data"})
                if last and last.presentation and last.presentation.chart_spec
                else None
            ),
            "observations": state.observations[-3:],
        }
        prompt = (
            "You are a business analytics assistant. Return ONE JSON object with keys "
            "action, intent, tool, instruction, answer. action is ANSWER, CLARIFY or CALL_TOOL. "
            "Tools: list_tables/search_schema/describe_table/get_relationships/"
            "get_column_profile inspect allowlisted metadata; get_available_metrics/"
            "get_business_context return approved definitions; ask_data delegates new or "
            "modified analytical questions to governed Text-to-SQL; analyze_result/"
            "present_result inspect bounded accepted result rows; check_sql_correctness "
            "reviews accepted SQL; change_visualization reuses or requeries as the "
            "existing gate decides; explain_existing_result explains accepted evidence. "
            "Use ask_data for new or modified analytical questions; "
            "it alone may generate governed SQL. Use tools only when needed. Never invent "
            "database facts. Never ask tools to execute arbitrary SQL. For a tool call, put a "
            "short natural-language request in instruction. After sufficient observations, "
            "answer with only supported facts.\nContext: " + json.dumps(compact, default=str)
        )
        provider = os.environ.get("TEXT2SQL_ASSISTANT_PROVIDER") or self._request.provider
        model = os.environ.get("TEXT2SQL_ASSISTANT_MODEL") or self._request.model
        if provider not in self.completers:
            raise ValueError(f"Assistant provider {provider} is not configured.")
        completer = self.completers[provider]
        raw, usage = await self._budget.wait(
            completer.complete_detailed(model, prompt), kind="model"
        )
        value = raw.strip()
        if value.startswith("```"):
            value = value.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        decision = AgentDecision.model_validate_json(value)
        merged_usage = TokenUsage(
            input_tokens=(state.usage.input_tokens or 0) + (usage.input_tokens or 0),
            output_tokens=(state.usage.output_tokens or 0) + (usage.output_tokens or 0),
        )
        ledger = [
            *state.ledger,
            CallLedgerEntry(
                component="conversational_agent",
                kind="MODEL",
                status="SUCCEEDED",
                provider=provider,
                requested_model=model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            ),
        ]
        if decision.action != "CALL_TOOL":
            answer = decision.answer or "Please clarify what you would like to know."
            return state.model_copy(
                update={
                    "decision": decision,
                    "usage": merged_usage,
                    "ledger": ledger,
                    "response": self._plain(answer).model_copy(
                        update={
                            "clarification_required": decision.action == "CLARIFY",
                            "clarification_question": (
                                answer if decision.action == "CLARIFY" else None
                            ),
                            "token_usage": merged_usage,
                            "call_ledger": ledger,
                        }
                    ),
                }
            )
        if not decision.tool or state.steps >= 3:
            return state.model_copy(
                update={
                    "response": self._plain(
                        "I reached the investigation limit without enough evidence."
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
        question = state.decision.instruction or self._request.message
        facts: dict[str, Any]
        if tool in {
            "ask_data",
            "change_visualization",
            "check_sql_correctness",
            "explain_existing_result",
        }:
            # Never let the orchestrator paraphrase away a user literal, filter,
            # or correction before the governed SQL/conversation pipeline sees it.
            question = self._request.message
            if tool == "check_sql_correctness":
                question = "Check correctness of the previous SQL. " + question
            response = await self.sql_handler(
                self._request.model_copy(update={"message": question})
            )
            if tool != "ask_data" or state.decision.intent != "INVESTIGATE":
                return state.model_copy(update={"response": response, "steps": state.steps + 1})
            facts = {
                "tool": tool,
                "message": response.message,
                "columns": response.generation.columns if response.generation else [],
                "rows": response.generation.rows[:10] if response.generation else [],
            }
        elif tool in {"get_business_context", "get_available_metrics"}:
            output = self.bridge.get_business_context(
                MetadataToolInput(
                    db_id=self._request.db_id,
                    question=question
                    if tool == "get_business_context"
                    else "What metrics are available?",
                )
            )
            facts = {"tool": tool, "facts": output.facts, "evidence_id": output.evidence_id}
        elif tool in {
            "list_tables",
            "search_schema",
            "describe_table",
            "get_relationships",
            "get_column_profile",
        }:
            schema_question = question
            if tool == "list_tables":
                schema_question = "What tables are available?"
            elif tool == "get_relationships":
                schema_question = "What relationships exist for " + question
            elif tool in {"describe_table", "get_column_profile"}:
                schema_question = "Which columns describe " + question
            self._budget.claim("database")
            output = await self.bridge.search_schema(
                MetadataToolInput(db_id=self._request.db_id, question=schema_question)
            )
            facts = {"tool": tool, "facts": output.facts, "evidence_id": output.evidence_id}
        elif tool in {"analyze_result", "present_result"}:
            last = self.get_last_response(self._request.session_id, self._request.db_id)
            facts = {
                "tool": tool,
                "summary": last.message if last else "No accepted result.",
                "columns": last.generation.columns if last and last.generation else [],
                "rows": last.generation.rows[:10] if last and last.generation else [],
            }
        else:
            facts = {"tool": tool, "error": "Unsupported tool."}
        return state.model_copy(
            update={
                "observations": [*state.observations, facts],
                "decision": None,
                "steps": state.steps + 1,
            }
        )
