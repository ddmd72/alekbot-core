"""The fact profile step's wiring (fact profile RFC §4.4): the knob, the lazy atlas, the builder, and the one instance the
production graph hands to both leased runs."""
import asyncio
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.composition import fact_profile_setup
from src.composition.fact_profile_setup import DEFAULT_MAX_PER_RUN, LazyAtlas, build_fact_profile_service, max_per_run
from src.domain.place_lookup import PlaceRow
from src.domain.place_names import PlaceNames, atlas_digest
from src.infrastructure.agent_config import FACT_PROFILER
from src.services.fact_profile_service import FactProfileService
from tests.support.prod_graph import ProdGraph, prod_graph  # noqa: F401  (fixture)

ROWS = [PlaceRow(41.9028, 12.4964, "Rome", "Roma", "Italy", 2_800_000, geonameid="1")]


@pytest.mark.parametrize("raw,expected", [
    (None, 20), ("", 20), ("   ", 20), ("0", 0), ("7", 7), (" 35 ", 35), ("1000", 1000),
])
def test_the_knob_is_an_int_from_zero_up_and_unset_means_twenty(raw, expected):
    assert max_per_run(raw) == expected and DEFAULT_MAX_PER_RUN == 20


@pytest.mark.parametrize("raw", ["abc", "-1", "2.5", "ten"])
def test_anything_else_is_twenty_with_one_warning(raw, caplog):
    with caplog.at_level(logging.WARNING):
        assert max_per_run(raw) == 20
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_a_valid_value_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING):
        max_per_run("5")
    assert caplog.records == []


async def test_the_atlas_loads_once_across_two_concurrent_calls_and_off_the_event_loop(monkeypatch):
    loads = []
    loop = asyncio.get_running_loop()
    main_thread = threading.get_ident()

    def loader():
        loads.append(threading.get_ident())
        return list(ROWS)

    monkeypatch.setattr(fact_profile_setup, "load_place_rows", loader)
    atlas = LazyAtlas()

    first, second = await asyncio.gather(atlas(), atlas())
    third = await atlas()

    assert len(loads) == 1 and loads[0] != main_thread and loop is not None
    assert first is second is third
    names, digest = first
    assert isinstance(names, PlaceNames) and digest == atlas_digest(ROWS)


def test_the_atlas_is_not_loaded_at_construction(monkeypatch):
    def forbidden():
        raise AssertionError("loaded at boot")

    monkeypatch.setattr(fact_profile_setup, "load_place_rows", forbidden)
    build_fact_profile_service(SimpleNamespace(repository=object(), fact_profiler=object(), embedding_service=object(),
                               confirmed_people=object()),
                               max_per_run=3)


def test_the_builder_wires_the_containers_ports_and_twice_the_call_timeout():
    container = SimpleNamespace(repository=object(), fact_profiler=object(), embedding_service=object(),
                               confirmed_people=object())

    service = build_fact_profile_service(container, max_per_run=None)

    assert isinstance(service, FactProfileService)
    assert (service._repo, service._profiler, service._embeddings) == (
        container.repository, container.fact_profiler, container.embedding_service)
    assert service._max_per_run is None and service._concurrency == 4
    assert service._call_budget_s == 2 * FACT_PROFILER.call_timeout_s


@pytest.mark.slow
async def test_the_graph_wires_one_instance_into_both_services(prod_graph: ProdGraph):
    app = prod_graph.app
    consolidation = app.worker_handler._consolidation
    review = app.worker_handler._fact_review

    assert isinstance(consolidation._profile_step, FactProfileService)
    assert consolidation._profile_step is review._profile_step
    assert consolidation._profile_step._repo is app.container.repository
    assert consolidation._profile_step._profiler is app.container.fact_profiler
    assert consolidation._profile_step._max_per_run == 20


def test_the_graph_reads_the_knob_with_os_getenv_at_its_call_site():
    graph = (Path(__file__).resolve().parents[3] / "src" / "composition" / "application_graph.py").read_text()
    assert 'profile_max_per_run(os.getenv("FACT_PROFILER_MAX_PER_RUN"))' in graph
    assert "FACT_PROFILER_MAX_PER_RUN" not in (Path(__file__).resolve().parents[3] / "src" / "config" / "settings.py").read_text()
