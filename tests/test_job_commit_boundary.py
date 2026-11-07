"""Job cancellation and progress must describe the exact accepted-state boundary."""
import asyncio
from pathlib import Path
from threading import Event
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from test_v63_conversation import ScriptedCompleter, ScriptedSQL, _client, _tool

from semantic_text2sql.models import ChatRequest
from semantic_text2sql.run_history import RunHistoryStore, RunRecord


def test_cancellation_is_declined_once_accepted_result_is_finalizing(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    entered, release = Event(), Event()

    class PausedHistory(RunHistoryStore):
        def record(self, entry: RunRecord) -> RunRecord:
            if entry.original_message == "Add currency category":
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test did not release history writer")
            return super().record(entry)

    async def scenario() -> None:
        top = ScriptedCompleter([
            {**_tool("NEW_QUERY", "ask_data", "Total consumption", operation="NEW_QUERY"),
             "finish_after_tool": True},
            {**_tool("MODIFY_QUERY", "ask_data", "Add currency category", operation="REFINE"),
             "finish_after_tool": True},
        ])
        history = PausedHistory(None)
        app = _client(tmp_path, monkeypatch, top, ScriptedCompleter([]), ScriptedSQL(), history).app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            payload = {"session_id": "commit", "db_id": "consumption",
                       "provider": "agentrouter", "model": "scripted"}
            first = await client.post("/api/chat", json={**payload, "message": "Total consumption"})
            assert first.status_code == 200
            response = await client.post("/api/chat/jobs", json={
                **payload, "message": "Add currency category",
            })
            job_id = response.json()["job_id"]
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                cancelled = await client.delete(f"/api/chat/jobs/{job_id}")
                assert cancelled.json()["status"] == "finalizing"
                release.set()
                for _ in range(100):
                    job = (await client.get(f"/api/chat/jobs/{job_id}")).json()
                    if job["status"] == "completed":
                        break
                    await asyncio.sleep(.01)
                assert job["status"] == "completed"
                assert job["response"]["history_saved"]
                assert job["response"]["generation"]["accepted"]
                assert len(history.list_sessions("commit")) == 2
            finally:
                release.set()

    asyncio.run(scenario())


def test_reset_during_finalization_cannot_restore_recent_result(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    entered, release = Event(), Event()

    class PausedHistory(RunHistoryStore):
        def record(self, entry: RunRecord) -> RunRecord:
            entered.set()
            release.wait(3)
            return super().record(entry)

    async def scenario() -> None:
        top = ScriptedCompleter([{
            **_tool("NEW_QUERY", "ask_data", "Total consumption", operation="NEW_QUERY"),
            "finish_after_tool": True,
        }])
        app = _client(tmp_path, monkeypatch, top, ScriptedCompleter([]),
                      ScriptedSQL(), PausedHistory(None)).app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            created = await client.post("/api/chat/jobs", json={
                "session_id": "reset-saving", "db_id": "consumption",
                "provider": "agentrouter", "model": "scripted", "message": "Total consumption",
            })
            identity = created.json()["job_id"]
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                reset = await client.post("/api/conversations/reset-saving/reset")
                assert reset.status_code == 204
                release.set()
                for _ in range(100):
                    job = (await client.get(f"/api/chat/jobs/{identity}")).json()
                    if job["status"] == "completed":
                        break
                    await asyncio.sleep(.01)
                assert job["status"] == "completed"
                page = await client.post("/api/results/page", json={
                    "session_id": "reset-saving", "db_id": "consumption",
                    "result_id": job["response"]["result_id"], "offset": 0, "page_size": 5,
                })
                assert page.status_code == 404
            finally:
                release.set()

    asyncio.run(scenario())


def test_queued_cancellation_has_a_frozen_terminal_timestamp(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    async def scenario() -> None:
        top = ScriptedCompleter([])
        app = _client(tmp_path, monkeypatch, top, ScriptedCompleter([]), ScriptedSQL()).app
        endpoints = {route.name: route.endpoint for route in app.routes
                     if hasattr(route, "endpoint")}
        created = await endpoints["start_chat_job"](ChatRequest(
            session_id="queued", db_id="consumption", message="Total consumption",
            provider="agentrouter", model="scripted",
        ))
        identity = created["job_id"]
        await endpoints["cancel_chat_job"](identity)
        first = await endpoints["get_chat_job"](identity)
        assert first["status"] == "cancelled"
        assert first.get("finished_at") is not None
        await asyncio.sleep(.02)
        second = await endpoints["get_chat_job"](identity)
        assert second["elapsed_ms"] == first["elapsed_ms"]
        assert top.calls == 0

    asyncio.run(scenario())


def test_job_capacity_limits_active_work_not_just_finished_history(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    monkeypatch.setenv("TEXT2SQL_MAX_JOBS", "10")

    async def scenario() -> None:
        app = _client(tmp_path, monkeypatch, ScriptedCompleter([]),
                      ScriptedCompleter([]), ScriptedSQL()).app
        endpoints = {route.name: route.endpoint for route in app.routes
                     if hasattr(route, "endpoint")}
        request = ChatRequest(session_id="capacity", db_id="consumption",
                              message="Total consumption", provider="agentrouter", model="scripted")
        jobs = []
        try:
            for _ in range(10):
                jobs.append(await endpoints["start_chat_job"](request.model_copy()))
            with pytest.raises(HTTPException) as failure:
                await endpoints["start_chat_job"](request.model_copy())
            assert failure.value.status_code == 429
        finally:
            for job in jobs:
                await endpoints["cancel_chat_job"](job["job_id"])
            await asyncio.sleep(0)

    asyncio.run(scenario())


def test_direct_request_does_not_change_another_jobs_progress(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        class BlockingSQL(ScriptedSQL):
            async def generate(self, **kwargs: Any) -> tuple[str, int]:
                if "currency" not in str(kwargs["question"]).casefold():
                    started.set()
                    await asyncio.Event().wait()
                return await super().generate(**kwargs)

        top = ScriptedCompleter([
            {**_tool("NEW_QUERY", "ask_data", "Total consumption", operation="NEW_QUERY"),
             "finish_after_tool": True},
            {**_tool("NEW_QUERY", "ask_data", "Consumption by currency", operation="NEW_QUERY"),
             "finish_after_tool": True},
        ])
        app = _client(tmp_path, monkeypatch, top, ScriptedCompleter([]), BlockingSQL()).app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            payload = {"session_id": "same", "db_id": "consumption",
                       "provider": "agentrouter", "model": "scripted"}
            created = await client.post("/api/chat/jobs", json={
                **payload, "message": "Total consumption",
            })
            job_id = created.json()["job_id"]
            try:
                await asyncio.wait_for(started.wait(), 3)
                before = (await client.get(f"/api/chat/jobs/{job_id}")).json()
                answer = await client.post("/api/chat", json={
                    **payload, "message": "Consumption by currency",
                })
                assert answer.status_code == 200, answer.text
                after = (await client.get(f"/api/chat/jobs/{job_id}")).json()
                assert after["stage"] == before["stage"]
                assert after["events"] == before["events"]
                assert after.get("understanding") == before.get("understanding")
            finally:
                await client.delete(f"/api/chat/jobs/{job_id}")
                await asyncio.sleep(0)

    asyncio.run(scenario())


def test_stalled_history_writer_cannot_leave_job_finalizing_forever(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    monkeypatch.setenv("TEXT2SQL_HISTORY_WRITE_TIMEOUT_SECONDS", "0.1")
    entered, release = Event(), Event()

    class PausedHistory(RunHistoryStore):
        def record(self, entry: RunRecord) -> RunRecord:
            entered.set()
            release.wait(3)
            return super().record(entry)

    async def scenario() -> None:
        top = ScriptedCompleter([{
            **_tool("NEW_QUERY", "ask_data", "Total consumption", operation="NEW_QUERY"),
            "finish_after_tool": True,
        }])
        app = _client(tmp_path, monkeypatch, top, ScriptedCompleter([]),
                      ScriptedSQL(), PausedHistory(None)).app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            created = await client.post("/api/chat/jobs", json={
                "session_id": "history-timeout", "db_id": "consumption",
                "provider": "agentrouter", "model": "scripted", "message": "Total consumption",
            })
            identity = created.json()["job_id"]
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                for _ in range(50):
                    job = (await client.get(f"/api/chat/jobs/{identity}")).json()
                    if job["status"] == "completed":
                        break
                    await asyncio.sleep(.01)
                assert job["status"] == "completed"
                assert job["response"]["generation"]["accepted"]
                assert not job["response"]["history_saved"]
            finally:
                release.set()
                await asyncio.sleep(.01)

    asyncio.run(scenario())
