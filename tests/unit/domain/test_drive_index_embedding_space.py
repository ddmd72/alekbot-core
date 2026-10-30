"""A card embedded in another space is stale (USER_DRIVE_INDEXING_RFC §12.3)."""
from dataclasses import replace

from src.domain.drive_card import card_hash
from src.domain.drive_index import DriveSyncState
from src.domain.drive_index_planner import reembed_in_space, schedule, stale_versions, with_card

from .test_drive_index_planner import NOW, ch, observe


def _carded(space):
    rec = observe(ch("a", "a.pdf")).upsert
    return with_card(rec, card_hash(rec), space, NOW)


def test_a_card_in_another_space_is_stale():
    assert not stale_versions(_carded("s1"), embedding_space="s1")
    assert stale_versions(_carded("s1"), embedding_space="s2")
    assert not stale_versions(replace(_carded("s1"), has_card=False), embedding_space="s2")


def test_a_card_in_another_space_is_due_for_a_new_embedding_and_keeps_its_vector():
    rec = reembed_in_space(_carded("s1"), "s2")
    assert rec.has_card and rec.card_hash is None
    assert schedule(rec, NOW, first_seen=False).due_at == NOW
    same = _carded("s2")
    assert reembed_in_space(same, "s2") is same


def test_the_state_says_whether_its_cards_are_in_a_space():
    state = DriveSyncState(user_id="u", connection_id="u", applied_space="s1")
    assert state.cards_in_space("s1") and not state.cards_in_space("s2")
