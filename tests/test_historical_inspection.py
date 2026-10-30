from pathlib import Path

import pytest

from semantic_text2sql.run_history import RunHistoryStore, RunRecord


@pytest.mark.parametrize("count", [19, 20, 21, 40])
@pytest.mark.parametrize("disk", [False, True])
def test_retained_history_identity_and_completeness(
    tmp_path: Path, count: int, disk: bool,
) -> None:
    store = RunHistoryStore(tmp_path / "history.sqlite" if disk else None)
    record = RunRecord(
        run_id="run-one", session_id="owner", db_id="shop", operation="NEW_QUERY",
        original_message="List items", resolved_question="List items", provider="test",
        model="test", accepted=True, execution_status="EXECUTED", sql="SELECT id FROM items",
        columns=["id"], rows_preview=[[i] for i in range(min(count, 20))],
        row_count=count, created_at=1, response_payload={"result_id": "result-one"},
    )
    store.record(record)
    for reference in ["run-one", "result-one"]:
        inspected = store.inspect_result("owner", "shop", reference, offset=5, page_size=5)
        assert inspected is not None
        assert inspected["rows"] == [[i] for i in range(5, 10)]
        assert inspected["result_id"] == "result-one"
        assert inspected["complete"] is (count <= 20)
        assert inspected["calculation_allowed"] is False
        assert inspected["page_complete"] is False
        whole = store.inspect_result("owner", "shop", reference, page_size=100)
        assert whole is not None and whole["calculation_allowed"] is (count <= 20)
        assert bool(inspected["warning"]) is (count > 20)
    assert store.inspect_result("other", "shop", "result-one") is None
    assert store.inspect_result("owner", "other", "result-one") is None
    assert store.inspect_result("owner", "shop", "result-one", conversation_id="other") is None
    assert store.get("run-one", "owner").sql == record.sql  # type: ignore[union-attr]
    store.drop_session("owner")
    assert store.inspect_result("owner", "shop", "result-one") is None


def test_failed_or_truncated_history_is_never_complete() -> None:
    store = RunHistoryStore(None)
    for accepted, reference in [(False, "failed"), (True, "truncated")]:
        store.record(RunRecord(
            run_id=reference, session_id="owner", db_id="shop", operation="NEW_QUERY",
            original_message="List", resolved_question="List", provider="test", model="test",
            accepted=accepted, execution_status="EXECUTED", columns=["id"],
            rows_preview=[[1]], row_count=1, truncated=True, created_at=1,
        ))
    assert store.inspect_result("owner", "shop", "failed") is None
    result = store.inspect_result("owner", "shop", "truncated")
    assert result is not None and not result["calculation_allowed"]


def test_memory_reset_boundary_survives_history_restart(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    store = RunHistoryStore(path)
    store.record(RunRecord(
        run_id="old", session_id="owner", db_id="shop", operation="NEW_QUERY",
        original_message="List", resolved_question="List", provider="test", model="test",
        accepted=True, execution_status="EXECUTED", columns=["id"], rows_preview=[[1]],
        row_count=1, created_at=1,
    ))
    store.mark_memory_reset("owner")
    reopened = RunHistoryStore(path)
    assert reopened.get("old", "owner") is not None  # Audit is retained.
    assert reopened.inspect_result("owner", "shop", "old") is None
