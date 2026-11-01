from __future__ import annotations

import asyncio
import copy
import json

import pytest
from pydantic import ValidationError
from test_conversational_agent import ScriptedStructuredCompleter, _decision_agent

from semantic_text2sql.conversational_agent import (
    AGENT_DECISION_SCHEMA,
    AgentDecision,
    AgentState,
    ToolInput,
    _scoped_decision_issues,
    _scoped_decision_schema,
)
from semantic_text2sql.llm import _ollama_decision_actions, _ollama_wire_schema
from semantic_text2sql.models import ChatRequest, ChatResponse


def test_ollama_wire_omits_only_large_grammar_bounds_not_local_validation():
    original = copy.deepcopy(AGENT_DECISION_SCHEMA)
    wire = _ollama_wire_schema(original)
    assert original == AGENT_DECISION_SCHEMA
    assert "maxLength" not in wire["properties"]["answer"]
    assert wire["properties"]["action"] == original["properties"]["action"]
    arguments = wire["properties"]["tool_arguments"]["anyOf"][0]["properties"]
    assert arguments["approval_quote"]["maxLength"] == 1000
    assert arguments["source_run_id"]["anyOf"][0]["pattern"]
    with pytest.raises(ValidationError):
        ToolInput(objective="x" * 4001)


def test_schema_scopes_sources_and_proposals_without_mutating_canonical_schema():
    context = {
        "accepted_analysis_reference": {"result_id": "current"},
        "dashboard_evidence": [{"result_id": "new"}],
        "current_dashboard": {
            "pending_proposal": {
                "proposal_id": "pending",
                "version": 3,
                "source_requirements": [{"result_id": "retained"}],
            }
        },
    }
    schema = _scoped_decision_schema(context)
    args = schema["properties"]["tool_arguments"]["anyOf"][0]["properties"]
    assert args["source_result_ids"]["items"]["enum"] == ["current", "new", "retained"]
    assert args["proposal_id"]["enum"] == ["pending", None]
    assert args["proposal_version"]["enum"] == [3, None]
    assert (
        "enum"
        not in AGENT_DECISION_SCHEMA["properties"]["tool_arguments"]["anyOf"][0]["properties"][
            "source_result_ids"
        ]["items"]
    )
    empty = _scoped_decision_schema({})
    assert "ai_dashboard_designer" not in empty["properties"]["tool"]["anyOf"][0]["enum"]
    inactive = empty["properties"]["tool_arguments"]["anyOf"][0]["properties"]
    for name in ("proposal_id", "proposal_version", "approval_quote"):
        assert inactive[name] == {"type": "null", "enum": [None]}
    original = AGENT_DECISION_SCHEMA["properties"]["tool_arguments"]["anyOf"][0]["properties"]
    assert original["proposal_version"]["minimum"] == 1


def test_native_llama_grammar_couples_action_and_tool_fields():
    schema = _ollama_wire_schema(_scoped_decision_schema({}))
    consistent = _ollama_decision_actions(schema)
    answer, tool, ongoing = consistent["anyOf"]
    assert answer["properties"]["action"]["enum"] == ["ANSWER"]
    assert answer["properties"]["tool"] == {"type": "null"}
    assert answer["properties"]["tool_arguments"] == {"type": "null"}
    assert tool["properties"]["action"]["enum"] == ["CALL_TOOL"]
    assert tool["properties"]["tool"]["type"] == "string"
    assert tool["properties"]["tool_arguments"]["type"] == "object"
    assert tool["properties"]["answer"] == {"type": "null"}
    assert "ai_dashboard_designer" not in tool["properties"]["tool"]["enum"]
    assert "undo_dashboard" not in tool["properties"]["tool"]["enum"]
    assert next(iter(tool["properties"])) == "result_kind"
    assert tool["properties"]["result_kind"]["enum"] == ["ANALYSIS", "CHART"]
    assert tool["properties"]["tool_arguments"]["properties"]["dashboard_goal"] == {
        "type": "null",
    }
    assert ongoing["properties"]["result_kind"]["enum"] == ["DASHBOARD"]
    assert ongoing["properties"]["finish_after_tool"]["enum"] == [False]
    assert ongoing["properties"]["tool_arguments"]["properties"]["dashboard_goal"]["type"] == [
        "string", "null",
    ]


def test_invalid_source_reference_is_repaired_before_any_tool_executes(registry, tmp_path):
    from test_dashboard import snapshot

    first = {
        "action": "CALL_TOOL",
        "intent": "VISUALIZE",
        "tool": "ai_dashboard_designer",
        "tool_arguments": {"objective": "Propose layout", "source_result_ids": ["invented"]},
    }
    corrected = copy.deepcopy(first)
    corrected["tool_arguments"]["source_result_ids"] = ["result"]
    completer = ScriptedStructuredCompleter([json.dumps(first), json.dumps(corrected)])
    agent = _decision_agent(registry, tmp_path, completer, [])
    calls = []
    agent.get_last_response = lambda *_: snapshot()

    async def design(request, objective, source, undo):
        calls.append(request._dashboard_source_ids)
        return ChatResponse(
            session_id=request.session_id,
            operation="VISUALIZE",
            resolved_question=objective,
            message="Review proposal",
            dashboard_design_proposal={"status": "AWAITING_APPROVAL"},
        )

    agent.dashboard_handler = design
    response = asyncio.run(
        agent.run(ChatRequest(session_id="owner", db_id="sales", message="Design a dashboard"))
    )
    assert calls == [["result"]] and len(completer.calls) == 2
    assert response.call_ledger[0].status == "FAILED"
    assert "source_result_ids" in response.call_ledger[0].validation_issues[0]
    assert response.dashboard is None


def test_unknown_ids_never_become_authorized_or_silently_replaced():
    decision = AgentDecision(
        action="CALL_TOOL",
        intent="VISUALIZE",
        tool="ai_dashboard_designer",
        tool_arguments=ToolInput(objective="Design", source_result_ids=["bad"]),
    )
    issues = _scoped_decision_issues(
        decision, {"accepted_analysis_reference": {"result_id": "good"}}
    )
    assert issues and decision.tool_arguments.source_result_ids == ["bad"]


def test_approval_quote_is_constrained_and_paraphrases_require_repair():
    context = {
        "question": "I confirm this design.",
        "current_dashboard": {
            "pending_proposal": {
                "proposal_id": "pending",
                "version": 3,
                "source_requirements": [{"result_id": "data"}],
            }
        },
    }
    schema = _scoped_decision_schema(context)
    args = schema["properties"]["tool_arguments"]["anyOf"][0]["properties"]
    assert args["approval_quote"]["enum"] == ["I confirm this design.", None]
    decision = AgentDecision(
        action="CALL_TOOL",
        intent="VISUALIZE",
        tool="ai_dashboard_designer",
        tool_arguments=ToolInput(
            objective="Confirm",
            design_action="APPROVE",
            proposal_id="pending",
            proposal_version=3,
            approval_quote="I confirm this design",
        ),
    )
    assert _scoped_decision_issues(decision, context)
    decision.tool_arguments.approval_quote = context["question"]
    assert not _scoped_decision_issues(decision, context)


def test_scoped_sql_handoff_retains_original_user_constraints(registry, tmp_path):
    agent = _decision_agent(registry, tmp_path, ScriptedStructuredCompleter([]), [])
    captured = []

    async def sql(request):
        captured.append(request)
        return ChatResponse(
            session_id=request.session_id,
            operation="NEW_QUERY",
            resolved_question=request.message,
            message="No query executed in fixture",
        )

    agent.sql_handler = sql
    original = ChatRequest(
        session_id="owner", db_id="shop", message="Use all dates and no extra filters or joins."
    )
    runner = agent._for_request(original)
    state = AgentState(
        decision=AgentDecision(
            action="CALL_TOOL",
            intent="NEW_QUERY",
            tool="ask_data",
            tool_arguments=ToolInput(objective="Total by month", operation="NEW_QUERY"),
        )
    )
    asyncio.run(runner._act(state))
    evidence = json.loads(captured[0].evidence)
    assert evidence["original_client_request"] == original.message
    assert evidence["scoped_analytical_objective"] == "Total by month"
    assert captured[0].message == "Total by month"
