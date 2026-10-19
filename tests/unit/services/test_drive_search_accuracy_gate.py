"""The search side of the migration gate (USER_DRIVE_INDEXING_RFC §26.7, Δ65): the gate is the `accuracy` stamp, and
the coverage line's "re-keying" reads the `keys` stamp, not the gate key."""
from dataclasses import replace

from src.domain.drive_index import KEYS_VERSION
from src.domain.drive_search import render_results

from .drive_search_v2_fixture import V2World


def _stamp(w: V2World, **versions) -> None:
    w.repo.states["u"] = replace(w.repo.states["u"], applied_versions=versions)


async def test_an_index_with_the_keys_stamp_but_not_the_accuracy_one_is_being_rebuilt():
    w = V2World(migrated=False)
    _stamp(w, keys=KEYS_VERSION, profile=2, passages=1)   # a v2.0 index, before the accuracy key
    w.file("Rome tickets.pdf")
    result = await w.search("rome tickets")
    assert result.awaiting_rebuild and result.cards == ()


async def test_the_gate_key_does_not_stand_in_for_the_keys_version():
    w = V2World(migrated=False)
    _stamp(w, accuracy=1, keys=KEYS_VERSION)
    w.file("Rome tickets.pdf")
    result = await w.search("rome tickets")
    assert not result.awaiting_rebuild and not result.tiered.coverage.rekeying


async def test_an_older_keys_stamp_is_rekeying_under_the_gate_of_a_current_index():
    w = V2World(migrated=False)
    _stamp(w, accuracy=1, keys=KEYS_VERSION - 1)
    w.file("Rome tickets.pdf")
    result = await w.search("rome tickets")
    assert result.tiered.coverage.rekeying and render_results(result)
