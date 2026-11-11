"""The fact search is wired once in the application graph (fact profile RFC §5): one service over the graph's fact
repository and embedding service; the place table is read once, at service start, never at build."""
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


async def test_the_place_table_is_not_loaded_at_build_then_once_at_start(monkeypatch):
    """Owner decision 2026-11-10 (cold-instance timeout): the table loads at service start through the one shared
    LazyAtlas, never at build, and a search reads that copy instead of loading the table again."""
    from src.composition import fact_profile_setup, fact_search_setup
    from src.composition.application_graph import ApplicationGraph
    loads = []
    # Both loaders count into one list: the shared atlas's and the search's own fallback, so a second read shows.
    monkeypatch.setattr(fact_profile_setup, "load_place_rows", lambda: loads.append(1) or [])
    monkeypatch.setattr(fact_search_setup, "load_place_rows", lambda: loads.append(1) or [])
    loads_when_start_began = []
    real_start = ApplicationGraph.start

    async def start(self):
        loads_when_start_began.append(len(loads))
        await real_start(self)

    monkeypatch.setattr(ApplicationGraph, "start", start)
    graph = await prod_graph_module.build(monkeypatch)
    try:
        assert loads_when_start_began == [0]
        assert loads == [1]
        search = graph.app.fact_search
        await search._place_keys(query(places=[("Rome", "")]))
        await search._place_keys(query(places=[("Rome", "")]))
        assert loads == [1]
    finally:
        await graph.app.shutdown()

