"""A late writer cannot resurrect a session after an explicit history clear."""
import time
from pathlib import Path

import pytest

from semantic_text2sql.run_history import RunHistoryStore, RunRecord


@pytest.mark.parametrize("disk", [False, True])
def test_clear_rejects_older_pending_writes_across_store_instances(
    tmp_path: Path, disk: bool,
) -> None:
    path = tmp_path / "history.sqlite" if disk else None
    writer = RunHistoryStore(path)
    clearer = RunHistoryStore(path) if disk else writer
    pending = RunRecord(
        run_id="pending", session_id="owner", db_id="shop", operation="NEW_QUERY",
        original_message="Total", resolved_question="Total", provider="test", model="test",
        accepted=True, execution_status="ACCEPTED", sql="SELECT 1", created_at=time.time(),
    )
    clearer.drop_session("owner")
    with pytest.raises(ValueError, match="cleared"):
        writer.record(pending)
    fresh = pending.model_copy(update={"run_id": "fresh", "created_at": time.time()})
    writer.record(fresh)
    assert [record.run_id for record in clearer.list_sessions("owner")] == ["fresh"]
