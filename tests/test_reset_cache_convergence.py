"""Regression coverage for one reset-cache contract across entry points."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from test_v63_conversation import ScriptedCompleter, ScriptedSQL, _client, _tool

import semantic_text2sql.api as api
from semantic_text2sql.agent import TextToSQLAgent
from semantic_text2sql.api import clear_reset_caches, create_app


class _Proposals:
    def __init__(self) -> None:
        self.cleared: list[str] = []

    def clear(self, session_id: str) -> None:
        self.cleared.append(session_id)


class _Revisions:
    def __init__(self) -> None:
        self.cleared: list[str] = []

    def clear(self, session_id: str) -> None:
        self.cleared.append(session_id)


def test_clear_reset_caches_removes_every_shared_cache_entry() -> None:
    proposals = _Proposals()
    revisions = _Revisions()
    bundles = {
        ("reset", "shop", "result"): {},
        ("other", "shop", "result"): {},
    }
    recent = {
        ("reset", "shop"): object(),
        ("other", "shop"): object(),
    }

    clear_reset_caches(
        "reset",
        design_proposals=proposals,  # type: ignore[arg-type]
        dashboard_revisions=revisions,  # type: ignore[arg-type]
        dashboard_bundles=bundles,  # type: ignore[arg-type]
        recent_results=recent,  # type: ignore[arg-type]
    )

    assert proposals.cleared == ["reset"]
    assert revisions.cleared == ["reset"]
    assert list(bundles) == [("other", "shop", "result")]
    assert list(recent) == [("other", "shop")]


def test_http_and_legacy_reset_use_the_shared_cache_clearer(
    registry: Any, monkeypatch: Any,
) -> None:
    calls: list[str] = []
    original = api.clear_reset_caches

    def spy(session_id: str, **kwargs: Any) -> None:
        calls.append(session_id)
        original(session_id, **kwargs)

    monkeypatch.setattr(api, "clear_reset_caches", spy)
    monkeypatch.setenv("TEXT2SQL_DATABASE_ROOT", str(registry.root))
    client = TestClient(create_app(TextToSQLAgent(registry, ScriptedSQL())))
    assert client.post("/api/conversations/http-reset/reset").status_code == 204
    response = client.post("/api/chat", json={
        "session_id": "legacy-reset", "db_id": "shop",
        "provider": "agentrouter", "model": "test-model", "message": "Start over",
    })
    assert response.status_code == 200
    assert response.json()["operation"] == "RESET_CONTEXT"
    assert calls == ["http-reset", "legacy-reset"]


def test_agent_reset_tool_uses_the_shared_cache_clearer(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    calls: list[str] = []
    original = api.clear_reset_caches

    def spy(session_id: str, **kwargs: Any) -> None:
        calls.append(session_id)
        original(session_id, **kwargs)

    monkeypatch.setattr(api, "clear_reset_caches", spy)
    top = ScriptedCompleter([_tool("RESET_CONTEXT", "reset_context", "Reset")])
    with _client(tmp_path, monkeypatch, top, ScriptedCompleter([]), ScriptedSQL()) as client:
        response = client.post("/api/chat", json={
            "session_id": "tool-reset", "db_id": "consumption",
            "provider": "agentrouter", "model": "scripted",
            "message": "Reset the conversation",
        })
        assert response.status_code == 200, response.text
        assert response.json()["operation"] == "RESET_CONTEXT"
    assert calls == ["tool-reset"]
