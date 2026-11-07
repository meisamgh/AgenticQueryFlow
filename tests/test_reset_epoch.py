"""Reset invalidates even an in-flight first turn with no accepted version yet."""
import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import Response
from test_v63_conversation import ScriptedCompleter, ScriptedSQL, _client, _tool

from semantic_text2sql.conversation import ConversationConflict, ConversationStore
from semantic_text2sql.models import ChatRequest, ConversationState


@pytest.mark.parametrize("top_enabled", [True, False])
def test_reset_before_first_commit_invalidates_old_turn_but_allows_new_question(
    tmp_path: Path, monkeypatch: Any, top_enabled: bool,
) -> None:
    async def scenario() -> None:
        started, release = asyncio.Event(), asyncio.Event()

        class PausedSQL(ScriptedSQL):
            first = True

            async def generate(self, **kwargs: Any) -> tuple[str, int]:
                if self.first:
                    self.first = False
                    started.set()
                    await release.wait()
                return await super().generate(**kwargs)

        top = ScriptedCompleter([{
            **_tool("NEW_QUERY", "ask_data", "Total consumption", operation="NEW_QUERY"),
            "finish_after_tool": True,
        }] * 2)
        app = _client(tmp_path, monkeypatch, top, ScriptedCompleter([]), PausedSQL()).app
        monkeypatch.setenv("TEXT2SQL_CONVERSATIONAL_AGENT_ENABLED", str(top_enabled).lower())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            payload = {"session_id": "first-reset", "db_id": "consumption",
                       "provider": "agentrouter", "model": "scripted",
                       "message": "Total consumption"}
            created = await client.post("/api/chat/jobs", json=payload)
            identity = created.json()["job_id"]
            try:
                await asyncio.wait_for(started.wait(), 3)
                reset = await client.post("/api/conversations/first-reset/reset")
                assert reset.status_code == 204
                release.set()
                for _ in range(100):
                    job = (await client.get(f"/api/chat/jobs/{identity}")).json()
                    if job["status"] in {"failed", "completed"}:
                        break
                    await asyncio.sleep(.01)
                assert job["status"] == "failed", job
                history = await client.get("/api/history", params={"session_id": "first-reset"})
                assert history.json() == []
                fresh = await client.post("/api/chat", json=payload)
                assert fresh.status_code == 200, fresh.text
                assert fresh.json()["generation"]["accepted"]
            finally:
                release.set()

    asyncio.run(scenario())


@pytest.mark.parametrize("disk", [False, True])
def test_reset_witness_is_durable_and_first_turn_version_cas_still_applies(
    tmp_path: Path, disk: bool,
) -> None:
    path = tmp_path / "state.sqlite" if disk else None
    reader = ConversationStore(path)
    writer = ConversationStore(path) if disk else reader
    state = ConversationState(session_id="one", db_id="shop", root_question="Total",
                              resolved_question="Total", last_sql="SELECT 1")
    old_epoch = reader.reset_epoch("one")
    writer.reset("one")
    assert reader.get("one") is None
    with pytest.raises(ConversationConflict, match="reset"):
        reader.put(state, expected_version=None, expected_epoch=old_epoch)
    current_epoch = reader.reset_epoch("one")
    assert current_epoch == old_epoch + 1
    saved = reader.put(state, expected_version=None, expected_epoch=current_epoch)
    assert saved.version == 1
    with pytest.raises(ConversationConflict, match="version"):
        writer.put(state, expected_version=None, expected_epoch=current_epoch)


def test_agent_can_explicitly_reset_then_ask_data_in_its_own_turn(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    top = ScriptedCompleter([
        _tool("RESET_CONTEXT", "reset_context", "Reset conversation"),
        {**_tool("NEW_QUERY", "ask_data", "Total consumption", operation="NEW_QUERY"),
         "finish_after_tool": True},
    ])
    with _client(tmp_path, monkeypatch, top, ScriptedCompleter([]), ScriptedSQL()) as client:
        response = client.post("/api/chat", json={
            "session_id": "same-turn-reset", "db_id": "consumption", "provider": "agentrouter",
            "model": "scripted", "message": "Reset the conversation and show total consumption",
        })
        assert response.status_code == 200, response.text
        assert response.json()["generation"]["accepted"], response.json()
        assert response.json()["history_saved"]


def test_queued_request_invalidated_before_it_starts_uses_no_model_calls(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    async def scenario() -> None:
        top = ScriptedCompleter([])
        app = _client(tmp_path, monkeypatch, top, ScriptedCompleter([]), ScriptedSQL()).app
        endpoints = {route.name: route.endpoint for route in app.routes
                     if hasattr(route, "endpoint")}
        created = await endpoints["start_chat_job"](ChatRequest(
            session_id="queued-reset", db_id="consumption", message="Total consumption",
            provider="agentrouter", model="scripted",
        ))
        await endpoints["reset_conversation"](Response(), "queued-reset")
        await asyncio.sleep(.01)
        job = await endpoints["get_chat_job"](created["job_id"])
        assert job["status"] == "failed"
        assert "reset" in job["error"]
        assert top.calls == 0

    asyncio.run(scenario())


def test_reset_witness_cannot_be_supplied_by_a_client(tmp_path: Path, monkeypatch: Any) -> None:
    with _client(tmp_path, monkeypatch, ScriptedCompleter([]),
                 ScriptedCompleter([]), ScriptedSQL()) as client:
        response = client.post("/api/chat", json={
            "session_id": "forged", "db_id": "consumption", "message": "Total consumption",
            "_reset_epoch": 999,
        })
        assert response.status_code == 422
