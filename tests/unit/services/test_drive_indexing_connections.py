"""The drive tick per connection (MULTI_DRIVE_RFC §8, §10, I3, I8): each connection's tick loads its connection,
indexes its own drive under its own keys and continues under its own payload; a purge removes one connection's index
only; the user-scoped transcript store is kept whenever another connection may read it, and a disconnect first retains
every head its records carry. Technical ids only."""
from dataclasses import replace

import pytest

from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_index import DriveSyncState
from src.domain.drive_transcript import RETAINED_DISCONNECT, StoredTranscript
from src.domain.user_drive import DriveReauthRequired

from .drive_multi_connection_world import USER, MultiWorld


@pytest.fixture
async def w():
    world = MultiWorld()
    await world.connect("conn-1", ref_tag="a1", drive_identity="fake_a:drive-1")
    await world.connect("conn-2", ref_tag="b2", drive_identity="fake_b:drive-2")
    return world


async def _index_one_item_on_each(w: MultiWorld) -> None:
    w.drives["conn-1"].add("data.bin", item_id="item-1")
    w.drives["conn-2"].add("data.bin", item_id="item-1")   # the same provider item id on the other connection
    await w.settle("conn-1")
    await w.settle("conn-2")


async def test_each_connection_indexes_its_own_drive_under_its_own_keys(w):
    await _index_one_item_on_each(w)
    one, two = w.records("conn-1")["item-1"], w.records("conn-2")["item-1"]
    assert (one.user_id, one.connection_id, one.identity) == (USER, "conn-1", "fake_a:drive-1:item-1")
    assert (two.user_id, two.connection_id, two.identity) == (USER, "conn-2", "fake_b:drive-2:item-1")
    states = {cid: s for cid, s in w.repo.states.items()}
    assert set(states) == {"conn-1", "conn-2"}
    assert states["conn-1"].drive_identity == "fake_a:drive-1" and states["conn-2"].drive_identity == "fake_b:drive-2"
    # every drive call went to the drive of the connection the tick was for
    assert {cid for _, cid in w.router.calls} == {"conn-1", "conn-2"}


async def test_a_continuation_carries_its_connection(w):
    """A busy lease requeues the tick of that connection: its dedup key and payload name the connection."""
    from datetime import timedelta
    await w.repo.acquire_lease(w.scope("conn-1"), "other", w.clock.now, w.clock.now + timedelta(minutes=31),
                               create=True)
    assert (await w.tick("conn-1")).status == "busy"
    assert [(t["key"], t["payload"]) for t in w.queue.tasks] == [
        ("conn-1:cont:busy-other", {"user_id": USER, "connection_id": "conn-1"})]
    assert (await w.tick("conn-2")).status == "done"   # the other connection's lease is its own


async def test_turning_indexing_off_purges_that_connection_only_and_keeps_the_shared_store(w):
    await _index_one_item_on_each(w)
    await w.repo.put_transcript(USER, StoredTranscript(content_key="fam:d:1", transcript=None, last_used_at=w.clock.now))
    await w.connections.set(USER, "conn-1", indexing_enabled=False)
    result = await w.settle("conn-1")
    assert result.status == "purged"
    assert w.records("conn-1") == {} and "conn-1" not in w.repo.states
    assert list(w.records("conn-2")) == ["item-1"] and "conn-2" in w.repo.states
    # another connection reads the user's store: the purge of one connection never deletes it
    assert await w.repo.get_transcript(USER, "fam:d:1") is not None


async def test_the_purge_of_a_users_only_connection_deletes_the_store_as_before():
    w = MultiWorld()
    await w.connect("conn-1")
    w.drives["conn-1"].add("data.bin", item_id="item-1")
    await w.settle("conn-1")
    await w.repo.put_transcript(USER, StoredTranscript(content_key="fam:d:1", transcript=None, last_used_at=w.clock.now))
    await w.connections.set(USER, "conn-1", indexing_enabled=False)
    assert (await w.settle("conn-1")).status == "purged"
    assert await w.repo.get_transcript(USER, "fam:d:1") is None


async def test_a_disconnect_purges_its_index_and_retains_the_transcripts_its_records_carried(w):
    """I8 (E23, D9): the disconnect flow's tick purges with keep; every head a record of the connection carried is
    retained first, so the orphan sweep never deletes it; a head no record carried is not touched."""
    await _index_one_item_on_each(w)
    rec = w.records("conn-1")["item-1"]
    await w.repo.upsert_records([replace(rec, transcript_key="fam:d:1")])
    for key in ("fam:d:1", "fam:d:2"):
        await w.repo.put_transcript(USER, StoredTranscript(content_key=key, transcript=None, last_used_at=w.clock.now))
    await w.connections.disconnect(USER, "conn-1")
    assert (await w.settle("conn-1")).status == "purged"
    assert w.records("conn-1") == {} and "conn-1" not in w.repo.states
    kept = await w.repo.get_transcript(USER, "fam:d:1")
    assert (kept.retained_at, kept.retained_reason) == (w.clock.now, RETAINED_DISCONNECT)
    assert (await w.repo.get_transcript(USER, "fam:d:2")).retained_at is None
    assert list(w.records("conn-2")) == ["item-1"]


async def test_a_lone_disconnected_connection_keeps_the_store_too():
    w = MultiWorld()
    await w.connect("conn-1")
    w.drives["conn-1"].add("data.bin", item_id="item-1")
    await w.settle("conn-1")
    await w.repo.put_transcript(USER, StoredTranscript(content_key="fam:d:9", transcript=None, last_used_at=w.clock.now))
    await w.connections.disconnect(USER, "conn-1")
    assert (await w.settle("conn-1")).status == "purged"
    assert await w.repo.get_transcript(USER, "fam:d:9") is not None   # D9: paid transcripts outlive a disconnect


async def test_a_connection_that_lost_its_grant_is_flagged_on_the_connection(w):
    w.drives["conn-1"].add("data.bin", item_id="item-1")
    w.drives["conn-1"].raise_next = DriveReauthRequired("revoked")
    assert (await w.tick("conn-1")).status == "access_lost"
    assert (await w.connections.get(USER, "conn-1")).state is ConnectionState.NEEDS_RECONNECT
    assert (await w.connections.get(USER, "conn-2")).state is ConnectionState.ACTIVE


async def test_a_sync_state_of_an_unknown_connection_is_never_ticked(w):
    stray = w.scope("conn-9")
    await w.repo.save_sync_state(DriveSyncState(user_id=USER, connection_id="conn-9", epoch=4))
    assert (await w.svc.tick(stray)).status == "unknown_connection"
    assert w.repo.states["conn-9"].epoch == 4 and w.queue.tasks == []


async def test_no_state_and_no_connection_does_nothing(w):
    assert (await w.svc.tick(w.scope("conn-9"))).status == "disabled"
    assert w.repo.states.keys() == set() and w.queue.tasks == []


async def test_the_tick_spends_the_initial_build_of_the_connections_account(w):
    await _index_one_item_on_each(w)
    handle = (await w.connections.get(USER, "conn-1")).handle
    await w.svc._spend_usd(handle, "profile", 1.5, 0.0, 1)   # the money path every model call takes
    assert (await w.repo.get_initial_build(USER, "realm-x", "sub-conn-1")).spent_usd == 1.5
    assert (await w.repo.get_initial_build(USER, "realm-x", "sub-conn-2")).spent_usd == 0.0
    # no account (the credential is gone): nothing is covered by an initial build, the month carries it all
    w.accounts.pop("cred-conn-2")
    await w.svc._spend_usd((await w.connections.get(USER, "conn-2")).handle, "profile", 2.0, 0.0, 1)
    usage = await w.repo.get_model_usage(USER, "2026-10")
    assert usage.profile_usd == pytest.approx(3.5) and usage.profile_initial_usd == pytest.approx(1.5)


async def test_a_note_binds_by_identity_on_the_connection_whose_tag_it_carries():
    """MULTI_DRIVE_RFC §7.2, §9: two connections of one drive see the same item identity; the owner's note bound on
    conn-1 (its `r:a1:` key) is merged into conn-1's record only, found by `item_binding_key(identity)`."""
    from src.adapters.in_memory_drive_note_repo import InMemoryDriveNoteRepository
    from src.domain.drive_note import DriveNote, NoteState, item_binding_key, tagged_ref_key
    from .drive_index_fakes import Clock
    notes = InMemoryDriveNoteRepository(clock=Clock())
    w = MultiWorld(notes=notes)
    await w.connect("conn-1", ref_tag="a1", drive_identity="fake_a:drive-1")
    await w.connect("conn-2", ref_tag="b2", drive_identity="fake_a:drive-1")   # the same drive, connected twice
    identity = "fake_a:drive-1:item-1"
    notes.notes[(USER, "n-1")] = DriveNote(
        note_id="n-1", user_id=USER, account_id="acc-1", state=NoteState.ACTIVE, current_version=1,
        text="the quarterly plan", match_keys=[item_binding_key(identity), tagged_ref_key("a1", "item-1")],
        updated_at=w.clock.now, revision=1)
    await _index_one_item_on_each(w)
    one, two = w.records("conn-1")["item-1"], w.records("conn-2")["item-1"]
    assert one.identity == two.identity == identity
    assert one.owner_note_id == "n-1" and one.owner_note_text == "the quarterly plan"
    assert two.owner_note_id == "" and two.owner_note_text == ""
