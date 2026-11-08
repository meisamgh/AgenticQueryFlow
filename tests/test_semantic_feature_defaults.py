"""Current semantic features default on, while explicit opt-outs still work."""
from pathlib import Path
from typing import Any

import pytest
from test_kpi_first import SQLModel, execute, fixture, service

from semantic_text2sql.ontology import configured_local_ontology
from semantic_text2sql.recovery import ontology_recovery_allowed


def test_kpi_first_and_grounding_are_active_without_environment_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("TEXT2SQL_GRAPH_ENABLED", "TEXT2SQL_KPI_FIRST_ENABLED",
                "TEXT2SQL_KPI_GROUNDING_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    registry, document = fixture(tmp_path)
    model = SQLModel("SELECT SUM(amount)-SUM(refund_amount) AS net_revenue FROM orders "
                     "WHERE status='complete'")
    result = execute(service(tmp_path, registry, document, model), "Sales after refunds")
    assert result.generated.accepted and result.generated.rows == [[135.0]]
    assert result.semantic_contract.kpi_grounding.resolution.selected_ids == ["net_revenue"]
    assert model.calls == 1


@pytest.mark.parametrize("override", [None, "false"])
def test_local_recovery_and_graph_persistence_defaults_and_opt_out(
    tmp_path: Path, monkeypatch: Any, override: str | None,
) -> None:
    for key in ("TEXT2SQL_ONTOLOGY_RECOVERY_ENABLED", "TEXT2SQL_GRAPH_PERSISTENCE_ENABLED"):
        if override is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, override)
    registry, _ = fixture(tmp_path)
    monkeypatch.setenv("TEXT2SQL_GRAPH_ROOT", str(tmp_path / "graphs"))
    adapter = configured_local_ontology(registry.inspect("shop"), None)
    assert adapter.graphs.persistence_enabled == (override is None)
    assert ontology_recovery_allowed("CORRECTNESS_REVIEW") == (override is None)
    assert not ontology_recovery_allowed("SQL_PARSE_ERROR")
    assert not ontology_recovery_allowed("PROVIDER_TIMEOUT")
