"""A terminally refused card is a flag on the record, not a record state (USER_DRIVE_INDEXING_RFC §3.4, §8.2): the
state and reason stay what the structure says, and the refusal binds the refused card text only."""
import pytest
from src.domain import drive_card
from src.domain import drive_index_planner as planner
from src.domain.drive_index import Reason, RecordState
from src.domain.drive_index_params import MAX_ATTEMPTS, RETRY_CAP

from .test_drive_indexing_card_embedding_outcomes import (
    _bump_card_version, _count_upgrade_pages, _refuse_cards_starting_with,
)
from .test_drive_indexing_service import World
from .drive_index_fakes import U_SCOPE


async def _until_terminal(w):
    for _ in range(MAX_ATTEMPTS + 2):
        await w.settle()
        w.clock.advance(RETRY_CAP.total_seconds())


async def test_a_terminal_refusal_sets_the_flag_and_keeps_the_structural_state():
    w = World()
    bad = w.drive.add("weird.pdf")
    refused = _refuse_cards_starting_with(w, "weird")
    await _until_terminal(w)
    rec = w.rec(bad)
    assert len(refused) == MAX_ATTEMPTS
    assert rec.card_refused and not rec.has_card and bad not in w.carded()
    assert (rec.state, rec.reason) == (RecordState.METADATA_ONLY, Reason.KIND_NOT_ADMITTED)
    assert rec.due_at is None and rec.attempts == 0


async def test_the_flag_holds_on_re_emission_and_lifts_on_a_new_text():
    w = World()
    bad = w.drive.add("weird.pdf")
    refused = _refuse_cards_starting_with(w, "weird")
    await _until_terminal(w)
    w.drive.reemit(bad)
    await w.settle()
    assert len(refused) == MAX_ATTEMPTS and w.rec(bad).card_refused
    w.drive.rename(bad, "renamed.pdf")
    await w.settle()
    rec = w.rec(bad)
    assert not rec.card_refused and rec.has_card and rec.state is RecordState.METADATA_ONLY


@pytest.mark.slow
async def test_a_card_builder_bump_retries_and_converges(monkeypatch):
    w = World(page_size=500)
    for i in range(150):
        w.drive.add(f"doc{i:03d}.pdf")
    bad = w.drive.add("weird.pdf")
    await w.settle()
    target = _bump_card_version(monkeypatch)
    refused = _refuse_cards_starting_with(w, "weird")
    pages = _count_upgrade_pages(w)
    await _until_terminal(w)
    per_tick = []
    for _ in range(6):
        before = len(pages)
        w.clock.advance(600)
        await w.tick()
        per_tick.append(len(pages) - before)
    rec = w.rec(bad)
    assert len(refused) == MAX_ATTEMPTS and rec.card_refused and rec.card_version == drive_card.CARD_VERSION
    assert rec.state is RecordState.METADATA_ONLY and not planner.stale_versions(rec)
    assert dict((await w.repo.get_sync_state(U_SCOPE)).applied_versions) == target
    assert per_tick == [0] * 6
