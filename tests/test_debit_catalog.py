"""Glossary-derived candidates remain non-authoritative; formulas execute accurately."""
import sqlite3
from pathlib import Path

from semantic_text2sql.database import DatabaseRegistry
from semantic_text2sql.semantic_catalog import SemanticCatalogStore
from semantic_text2sql.semantic_explorer import ExplorerCache
from semantic_text2sql.semantic_graph import compile_graph


def test_debit_candidate_graph_and_formula_semantics(tmp_path: Path) -> None:
    db_id = "debit_card_specializing"
    (tmp_path / db_id).mkdir()
    with sqlite3.connect(tmp_path / db_id / f"{db_id}.sqlite") as connection:
        connection.executescript('''
            CREATE TABLE yearmonth(CustomerID INTEGER, Date TEXT, Consumption REAL,
                PRIMARY KEY(CustomerID, Date));
            CREATE TABLE transactions_1k(TransactionID INTEGER PRIMARY KEY,
                CustomerID INTEGER, Date TEXT, Price REAL, Amount INTEGER);
            INSERT INTO yearmonth VALUES(1,'201201',10),(1,'201202',-2);
            INSERT INTO transactions_1k VALUES
                (1,1,'2012-01-01',10,2),(2,1,'2012-01-02',30,3),(3,1,'2012-01-03',0,0);
        ''')
        catalog = SemanticCatalogStore(Path(__file__).parents[1] / "data/semantic_catalogs").load(
            db_id)
        assert catalog is not None and len(catalog.metrics) == 4
        expected = {"total_consumption": 8, "total_spending": 40,
                    "average_unit_price": 7.5, "weighted_average_unit_price": 8}
        for metric in catalog.metrics:
            assert metric.status == "inferred" and metric.approval is None
            result = connection.execute(
                f'SELECT {metric.formula} FROM "{metric.source_relation}"').fetchone()
            assert result is not None and result[0] == expected[metric.id]
    schema = DatabaseRegistry(tmp_path).inspect(db_id)
    snapshot = compile_graph(schema, catalog, None, None)
    index = ExplorerCache().get(snapshot, snapshot.network())
    overview = index.graph(view="kpi")
    assert {node.id for node in overview.nodes} == {
        "metric:total_consumption", "metric:total_spending", "metric:average_unit_price",
        "metric:weighted_average_unit_price", "table:yearmonth", "table:transactions_1k"}
    links = [edge for edge in overview.edges if edge.kind == "SOURCE_TABLE"]
    assert len(links) == 4 and all(edge.status == "suggested" for edge in links)
    assert all(edge.target == ("table:yearmonth" if edge.source == "metric:total_consumption"
                               else "table:transactions_1k") for edge in links)
    assert not any(edge.kind == "SOURCE_TABLE" for edge in snapshot.edges)
    assert all(node.kind != "COLUMN" for node in overview.nodes)
    expanded = index.graph(view="kpi", node_id="metric:average_unit_price", depth=1)
    assert {"USES_COLUMN", "HAS_COLUMN"} <= {edge.kind for edge in expanded.edges}
    assert "table:transactions_1k" in {node.id for node in expanded.nodes}
    filtered = index.graph(view="kpi", relations={"DEPENDS_ON"})
    assert all(edge.kind != "SOURCE_TABLE" for edge in filtered.edges)
    bounded = index.graph(view="kpi", node_limit=2, edge_limit=1)
    assert len(bounded.nodes) <= 2 and len(bounded.edges) <= 1 and bounded.truncated
    graph = index.graph(view="kpi", node_id="metric:average_unit_price", depth=2,
                        lineage=True, direction="upstream")
    assert {"metric:average_unit_price", "column:transactions_1k.Price",
            "column:transactions_1k.Amount", "table:transactions_1k"} <= {
                node.id for node in graph.nodes}
    assert all(node.status != "approved" for node in graph.nodes)
    assert all(not edge.authoritative for edge in graph.edges if edge.kind == "USES_COLUMN")
