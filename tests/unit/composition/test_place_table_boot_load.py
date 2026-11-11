"""The place table is read once, at service start, and the fact search shares that one object (a first search on a
fresh instance paid about 6 s for it). A failed boot load leaves the lazy path in place."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.composition import fact_profile_setup
from src.composition.application_graph import ApplicationGraph
from src.composition.fact_profile_setup import LazyAtlas
from src.composition.fact_search_setup import build_fact_search_service
from src.domain.place_lookup import PlaceRow

ROWS = [PlaceRow(40.2, -8.4, "Coimbra", "Coimbra", "PT", 5)]


@pytest.fixture
def table_reads(monkeypatch):
    reads = []

    def loader():
        reads.append(1)
        return list(ROWS)

    monkeypatch.setattr(fact_profile_setup, "load_place_rows", loader)
    return reads


def _graph(atlas):
    return SimpleNamespace(place_atlas=atlas, agent_task_queue=None, billing_agent=AsyncMock(),
                           logger_agent=AsyncMock(), agent_factory=AsyncMock())


async def test_start_loads_the_table_once_and_the_search_and_the_profile_step_share_it(table_reads):
    atlas = LazyAtlas()
    search = build_fact_search_service(object(), object(), object(), atlas=atlas)

    await ApplicationGraph.start(_graph(atlas))
    names_for_search = await search._names()
    profile_names, _ = await atlas()

    assert len(table_reads) == 1
    assert names_for_search is profile_names


async def test_a_failed_boot_load_is_logged_and_does_not_stop_the_start(monkeypatch):
    calls = []

    def failing_then_fine():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("table unreadable")
        return list(ROWS)

    monkeypatch.setattr(fact_profile_setup, "load_place_rows", failing_then_fine)
    atlas = LazyAtlas()
    graph = _graph(atlas)

    await ApplicationGraph.start(graph)

    graph.agent_factory.start.assert_awaited_once()
    assert atlas.names() is not None and len(calls) == 2  # the lazy path still loads it on first use
