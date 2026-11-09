"""The fact search is wired once in the application graph (fact profile RFC §5): one service over the graph's fact
repository and embedding service; the place table is read on the first search that names a place, never at build."""
from src.ports.fact_search_port import FactSearchPort
from src.services.fact_search_service import FactSearchService
from tests.support.fact_search_kit import query
from tests.support import prod_graph as prod_graph_module
from tests.support.prod_graph import prod_graph  # noqa: F401  (fixture)


async def test_the_graph_holds_one_fact_search_service_over_the_graphs_stores(prod_graph):
    search = prod_graph.app.fact_search
    assert isinstance(search, FactSearchService) and isinstance(search, FactSearchPort)
    assert search._repository is prod_graph.app.container.repository
    assert search._embedder is prod_graph.app.container.embedding_service


async def test_the_place_table_is_not_loaded_at_build_and_then_once(monkeypatch):
    from src.composition import fact_search_setup
    loads = []
    monkeypatch.setattr(fact_search_setup, "load_place_rows", lambda: loads.append(1) or [])
    graph = await prod_graph_module.build(monkeypatch)
    try:
        assert loads == []
        search = graph.app.fact_search
        await search._place_keys(query(places=[("Rome", "")]))
        await search._place_keys(query(places=[("Rome", "")]))
        assert loads == [1]
    finally:
        await graph.app.shutdown()

