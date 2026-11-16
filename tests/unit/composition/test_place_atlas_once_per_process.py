"""The place table is parsed once per process: a second LazyAtlas (a second graph built in the same process, as a test
process or a mutation child does many times) reuses the first one's names instead of parsing the table again. A
substituted loader gets its own entry, and a failed load is not kept."""
import pytest

from src.composition import fact_profile_setup
from src.composition.fact_profile_setup import LazyAtlas
from src.domain.place_lookup import PlaceRow

ROWS = [PlaceRow(40.2, -8.4, "Coimbra", "Coimbra", "PT", 5)]


def _counting_loader(reads):
    def loader():
        reads.append(1)
        return list(ROWS)
    return loader


async def test_two_atlases_of_one_process_read_the_table_once_and_hold_the_same_names(monkeypatch):
    reads = []
    monkeypatch.setattr(fact_profile_setup, "load_place_rows", _counting_loader(reads))

    first_names, first_digest = await LazyAtlas()()
    second_names, second_digest = await LazyAtlas()()

    assert len(reads) == 1
    assert second_names is first_names and second_digest == first_digest


async def test_a_substituted_loader_gets_its_own_table(monkeypatch):
    first_reads, second_reads = [], []
    monkeypatch.setattr(fact_profile_setup, "load_place_rows", _counting_loader(first_reads))
    first_names, _ = await LazyAtlas()()

    monkeypatch.setattr(fact_profile_setup, "load_place_rows", _counting_loader(second_reads))
    second_names, _ = await LazyAtlas()()

    assert len(first_reads) == 1 and len(second_reads) == 1
    assert second_names is not first_names


async def test_a_failed_load_is_not_kept_and_the_next_atlas_loads_the_table(monkeypatch):
    calls = []

    def failing_then_fine():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("table unreadable")
        return list(ROWS)

    monkeypatch.setattr(fact_profile_setup, "load_place_rows", failing_then_fine)
    with pytest.raises(OSError):
        await LazyAtlas()()

    names, _ = await LazyAtlas()()

    assert names is not None and len(calls) == 2
