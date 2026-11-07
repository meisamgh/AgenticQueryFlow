"""Extended pages must reauthorize the saved SQL before any re-execution."""
import sqlite3
from pathlib import Path
from typing import Any

from test_v63_conversation import ScriptedCompleter, ScriptedSQL, _client, _tool

from semantic_text2sql.database import DatabaseRegistry


def test_extended_page_blocks_new_wildcard_columns_before_execution(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    class WildcardSQL(ScriptedSQL):
        async def generate(self, **kwargs: Any) -> tuple[str, int]:
            self.calls.append(str(kwargs["question"]))
            return "SELECT * FROM yearmonth ORDER BY CustomerID, Date", 1

    top = ScriptedCompleter([{
        **_tool("NEW_QUERY", "ask_data", "Show all yearmonth data", operation="NEW_QUERY"),
        "finish_after_tool": True,
    }])
    with _client(tmp_path, monkeypatch, top, ScriptedCompleter([]), WildcardSQL()) as client:
        path = tmp_path / "consumption" / "consumption.sqlite"
        with sqlite3.connect(path) as connection:
            connection.executemany("INSERT INTO yearmonth VALUES(1, ?, ?)", [
                (f"{2025+i//12:04d}{i%12+1:02d}", float(i)) for i in range(600)
            ])
        first = client.post("/api/chat", json={
            "session_id": "pages", "db_id": "consumption", "provider": "agentrouter",
            "model": "scripted", "message": "Show all yearmonth data", "max_rows": 500,
        }).json()
        assert first["generation"]["accepted"], first
        with sqlite3.connect(path) as connection:
            connection.execute("ALTER TABLE yearmonth ADD COLUMN secret TEXT DEFAULT 'private'")

        def forbidden_execution(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("Saved wildcard re-executed before current-scope authorization")

        monkeypatch.setattr(DatabaseRegistry, "execute", forbidden_execution)
        response = client.post("/api/results/page", json={
            "session_id": "pages", "db_id": "consumption", "result_id": first["result_id"],
            "offset": 500, "page_size": 20,
        })
        assert response.status_code == 409
