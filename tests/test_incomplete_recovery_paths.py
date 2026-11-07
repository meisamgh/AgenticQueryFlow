"""Fallback probes must obey the same authorization and evidence boundaries."""
from semantic_text2sql.recovery import RecoveryTools


def test_fallback_cte_shadowing_cannot_probe_forbidden_table(registry, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tools = RecoveryTools(registry, "shop", registry.inspect("shop"), None)

    def forbidden_execution(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("Unauthorized fallback probe reached execution")

    monkeypatch.setattr(registry, "execute", forbidden_execution)
    assert tools.probe_filter_counts(
        "WITH orders AS (SELECT 1) SELECT amount FROM main.orders WHERE amount > 0",
        allowed_tables=["customers"],
    ) == []
    assert tools.database_probe_count == 0


def test_join_scoped_zero_is_not_global_entity_absence(registry) -> None:  # type: ignore[no-untyped-def]
    tools = RecoveryTools(registry, "shop", registry.inspect("shop"), None)
    checks = tools.probe_filter_counts(
        "SELECT c.name FROM customers c JOIN orders o "
        "ON c.customer_id=o.customer_id AND o.status='missing' WHERE c.customer_id=1",
        allowed_tables=["customers", "orders"],
    )
    assert checks[0]["match_count"] == 0
    assert "joined" in checks[0]["no_match_explanation"].lower()
