"""The embedding space is part of what makes a card stale (USER_DRIVE_INDEXING_RFC §12.3): the sync state records the
space its cards are in, a new space re-embeds every card through the upgrade pass, and until that pass finds no card
left in the old space the state says the card stream is not usable for the new one."""
import pytest
from src.domain.drive_index_params import MAX_ATTEMPTS, RETRY_CAP
from src.services import drive_indexing_service as svc_mod
from src.services.drive_indexing_service import DriveIndexingService

from .test_drive_indexing_card_embedding_outcomes import _refuse_cards_starting_with
from .test_drive_indexing_service import EXTRACTOR_VERSIONS, World
from .drive_index_fakes import LegacyConnections
from .drive_index_fakes import U_SCOPE


def _switch_space(w, space):
    w.svc = DriveIndexingService(w.drive, w.repo, w.emb, w.queue, w.users, lambda lat, lon: "Rome (Roma), Italy",
                                 space, extractors=w.svc._extractors, triage=w.svc._triage, connections=LegacyConnections(w.users, w.drive), clock=w.clock,
                                 monotonic=w.clock.monotonic)


async def test_onboarding_records_the_space_its_cards_are_in():
    w = World()
    w.drive.add("a.pdf")
    await w.settle()
    state = await w.repo.get_sync_state(U_SCOPE)
    assert state.applied_space == "test-space/8" and state.cards_in_space("test-space/8")
    assert not state.cards_in_space("other/768")


@pytest.mark.slow
async def test_a_new_space_re_embeds_every_card_and_then_applies():
    w = World(page_size=500)
    items = [w.drive.add(f"doc{i:03d}.pdf") for i in range(250)] + [w.drive.add("Photos", folder=True)]
    await w.settle()
    _switch_space(w, "new-space/768")
    w.emb.calls.clear()
    await w.tick()
    mid = await w.repo.get_sync_state(U_SCOPE)
    assert not mid.cards_in_space("new-space/768")
    await w.settle()
    state = await w.repo.get_sync_state(U_SCOPE)
    assert state.cards_in_space("new-space/768") and state.applied_space == "new-space/768"
    assert {w.rec(i).embedding_space for i in items} == {"new-space/768"}
    assert all(w.rec(i).has_card for i in items) and w.carded() == set(items)
    assert len(w.emb.calls) == len(items)


async def test_a_refused_card_does_not_hold_back_the_switch(monkeypatch):
    w = World()
    good = w.drive.add("a.pdf")
    bad = w.drive.add("weird.pdf")
    await w.settle()
    _switch_space(w, "new-space/768")
    refused = _refuse_cards_starting_with(w, "weird")
    for _ in range(MAX_ATTEMPTS + 2):
        await w.settle()
        w.clock.advance(RETRY_CAP.total_seconds())
    state = await w.repo.get_sync_state(U_SCOPE)
    assert len(refused) == MAX_ATTEMPTS and w.rec(bad).card_refused and bad not in w.carded()
    assert w.rec(good).embedding_space == "new-space/768" and state.cards_in_space("new-space/768")


async def test_a_space_change_alone_keeps_the_version_map(monkeypatch):
    w = World()
    w.drive.add("a.pdf")
    await w.settle()
    versions = dict((await w.repo.get_sync_state(U_SCOPE)).applied_versions)
    _switch_space(w, "new-space/768")
    await w.settle()
    assert dict((await w.repo.get_sync_state(U_SCOPE)).applied_versions) == versions == {**svc_mod.CURRENT_VERSIONS,
                                                                                     **EXTRACTOR_VERSIONS}
