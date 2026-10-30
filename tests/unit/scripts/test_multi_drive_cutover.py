"""The slice-1b cut-over script (MULTI_DRIVE_RFC §12, §10, T-10): a dry run writes nothing; --apply twice equals once;
the preflight refuses; the purge flags go first under the old lease and the purge deletes only `{user_id}:` ids; the
kept stores are untouched by the purge; every transcript part is copied and `last_used_at` stamped; --purge-new,
--copy-back and --cleanup do what the old and the new revision need. Run on the evaluating Firestore fake."""
from datetime import datetime

import pytest

from src.adapters.microsoft.onedrive_adapter import onedrive_drive_identity, onedrive_item_identity
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_index import sync_state_doc_id
from src.domain.drive_mirror import ledger_key, mirror_root_doc_id
from src.domain.drive_note import item_binding_key, tagged_ref_key
from src.domain.drive_ref import is_valid_tag
from src.domain.drive_transcript import transcript_doc_id
from src.domain.drive_usage import initial_build_doc_id
from tests.unit.scripts.multi_drive_cutover_world import (
    ACCOUNT, ARCHIVED_KEY, CRED, DAY, DRIVE, HASH_KEY, ITEM_KEY, NOW, ONEDRIVE, REALM, SUBJECT, USER, archive_key,
    later, world,
)


def _new_key(w, old_key):
    return w.script.new_content_key(old_key, DRIVE, ONEDRIVE)


IDENTITY = onedrive_drive_identity(DRIVE)


async def _applied():
    w = world()
    code, lines = await w.go("apply")
    assert code == 0, lines
    return w


# -- dry run ------------------------------------------------------------------------------------------------------

async def test_a_dry_run_writes_nothing_and_counts_per_collection():
    w = world()
    before = w.snapshot()
    code, lines = await w.go("dry-run", write=True)
    assert code == 0, lines
    assert w.snapshot() == before
    text = "\n".join(lines)
    assert "nothing written" in text
    n = w.names
    for expected in (f"deleted {n.records} 2", f"deleted {n.chunks} 2", f"deleted {n.hints} 2", f"deleted {n.jobs} 1",
                     f"deleted {n.batches} 1", f"deleted {n.sync_state} 1", f"copied {n.transcripts} 3",
                     f"copied {n.parts} 2", f"copied {n.roots} 1", f"copied {n.ledger} 6", f"tagged {n.notes} 1",
                     "connections created 1", "primary set 1"):
        assert expected in text, expected


async def test_the_dry_run_prints_the_computed_initial_build_key_with_its_spent_and_closed_state():
    w = world()
    _, lines = await w.go("dry-run")
    key = initial_build_doc_id(USER, REALM, SUBJECT)
    assert any(f"initial build -> {key[:8]}" in line and "open, spent $12.50 of $40.00" in line for line in lines)


# -- --apply ------------------------------------------------------------------------------------------------------

async def test_apply_creates_the_connection_and_the_primary_pointer():
    w = await _applied()
    (conn,) = await w.store.list_for_user(USER)
    assert (conn.kind, conn.credential_id, conn.account_id, conn.drive_identity) == (ONEDRIVE, CRED, ACCOUNT, IDENTITY)
    assert conn.mount_name == "Drive" and conn.state is ConnectionState.ACTIVE and is_valid_tag(conn.ref_tag)
    assert (conn.indexing_enabled, conn.transcribe_enabled) == (True, True)    # copied from UserBotConfig
    assert conn.created_at == NOW
    assert (await w.store.get_settings(USER)).primary_connection_id == conn.connection_id


async def test_the_flags_are_copied_as_they_are():
    w = world()
    w.col(w.names.users)[USER]["config"] = {"drive_indexing_enabled": True}
    await w.go("apply")
    (conn,) = await w.store.list_for_user(USER)
    assert (conn.indexing_enabled, conn.transcribe_enabled) == (True, False)


async def test_apply_purges_the_old_index_and_the_old_sync_state():
    w = await _applied()
    n = w.names
    for name in (n.records, n.chunks, n.hints, n.jobs, n.batches):
        assert not [i for i in w.col(name) if i.startswith(f"{USER}:")], name
    assert w.sub(n.jobs, f"{USER}:item-2", n.windows) == {}
    assert w.doc(n.sync_state, USER) is None


async def test_the_purge_deletes_only_ids_with_the_old_user_prefix():
    w = world()
    n = w.names
    scoped = {"user_id": USER, "connection_id": "connold1", "item_id": "item-1"}
    w.col(n.records)["connold1:item-1"] = dict(scoped)
    w.col(n.chunks)["connold1:item-1:1:0"] = dict(scoped)
    other = {"user_id": "usr-2", "item_id": "item-1"}
    w.col(n.records)["usr-2:item-1"] = dict(other)
    await w.go("apply")
    assert "connold1:item-1" in w.col(n.records) and "connold1:item-1:1:0" in w.col(n.chunks)
    assert "usr-2:item-1" in w.col(n.records)


async def test_the_purge_leaves_the_kept_stores_untouched():
    w = world()
    n = w.names
    kept = {name: dict(w.col(name)) for name in (n.roots, n.ledger, n.usage, n.credentials)}
    old_heads = {i: dict(d) for i, d in w.col(n.transcripts).items()}
    old_parts = dict(w.sub(n.transcripts, f"{USER}:{HASH_KEY}", n.parts))
    await w.go("apply")
    for name, docs in kept.items():
        for doc_id, data in docs.items():
            assert w.doc(name, doc_id) == data, (name, doc_id)
    for doc_id, data in old_heads.items():
        assert w.doc(n.transcripts, doc_id) == data
    assert w.sub(n.transcripts, f"{USER}:{HASH_KEY}", n.parts) == old_parts
    note = w.doc(n.notes, f"{USER}:n1")
    assert "r:item-1" in note["match_keys"] and f"i:{ONEDRIVE}:{DRIVE}:item-1" in note["match_keys"]


async def test_every_transcript_part_is_copied_and_last_used_at_stamped():
    w = await _applied()
    n = w.names
    new_id = transcript_doc_id(USER, _new_key(w, HASH_KEY))
    head = w.doc(n.transcripts, new_id)
    assert head["content_key"] == _new_key(w, HASH_KEY) and head["last_used_at"] == NOW
    assert head["summary"] == "a call" and head["parts"] == 2
    assert w.sub(n.transcripts, new_id, n.parts) == w.sub(n.transcripts, f"{USER}:{HASH_KEY}", n.parts)
    assert len(w.sub(n.transcripts, new_id, n.parts)) == 2
    fallback = w.doc(n.transcripts, transcript_doc_id(USER, _new_key(w, ITEM_KEY)))
    assert fallback["last_used_at"] == NOW


async def test_the_ledger_roots_initial_build_and_notes_are_copied_under_the_new_keys():
    w = await _applied()
    n = w.names
    (conn,) = await w.store.list_for_user(USER)
    root = w.doc(n.roots, mirror_root_doc_id(USER, IDENTITY))
    assert root["drive_identity"] == IDENTITY and root["root_item_id"] == "root-1"
    file_doc = w.doc(n.ledger, ledger_key(USER, "file", IDENTITY, onedrive_item_identity(DRIVE, "item-1")))
    assert file_doc["content_key"] == _new_key(w, HASH_KEY) and file_doc["drive_identity"] == IDENTITY
    archived = w.doc(n.ledger, ledger_key(USER, "file", IDENTITY, onedrive_item_identity(DRIVE, "item-9")))
    assert archived["content_key"] == _new_key(w, ARCHIVED_KEY) and archived["state"] == "archived"   # the pin
    for day, path in (("", "Archive"), (DAY, ""), (DAY, "Calls")):
        doc = w.doc(n.ledger, archive_key(IDENTITY, day, path))
        assert doc is not None and doc["source_item_id"] == archive_key(IDENTITY, day, path).rsplit(":", 1)[1]
    initial = w.doc(n.usage, initial_build_doc_id(USER, REALM, SUBJECT))
    assert (initial["spent_usd"], initial["allowance_usd"], initial["closed_at"]) == (12.5, 40.0, None)
    keys = w.doc(n.notes, f"{USER}:n1")["match_keys"]
    assert tagged_ref_key(conn.ref_tag, "item-1") in keys
    assert item_binding_key(onedrive_item_identity(DRIVE, "item-1")) in keys     # no re-binding (byte-identical)


async def test_apply_twice_equals_apply_once():
    w = await _applied()
    after_one = w.snapshot()
    code, lines = await w.go("apply", now=later(30))
    assert code == 0, lines
    assert w.snapshot() == after_one
    assert "already copied" in lines[-1] and "copied " + w.names.transcripts not in lines[-1].replace(
        "already copied", "")


# -- the preflight ------------------------------------------------------------------------------------------------

async def _refused(w, fragment):
    before = w.snapshot()
    code, lines = await w.go("apply")
    assert code == 1
    assert any(line.startswith("REFUSED") and fragment in line for line in lines), lines
    assert w.snapshot() == before
    return lines


async def test_refused_with_a_busy_lease():
    w = world()
    w.col(w.names.sync_state)[USER].update(lease_owner="tick-1", lease_until=later(5))
    await _refused(w, "lease is held")


async def test_a_lease_that_ran_out_is_free():
    w = world()
    w.col(w.names.sync_state)[USER].update(lease_owner="tick-1", lease_until=datetime(2026, 11, 2, 8, 0,
                                                                                       tzinfo=NOW.tzinfo))
    code, lines = await w.go("apply")
    assert code == 0, lines


@pytest.mark.parametrize("change, fragment", [
    (lambda w: setattr(w.infra, "tasks", 3), "holds 3 tasks"),
    (lambda w: setattr(w.infra, "paused", False), "queue is not paused"),
    (lambda w: w.infra.jobs.update({"start-drive-sync": False}), "start-drive-sync is not paused"),
    (lambda w: w.infra.missing.append("records (connection_id, item_id)"), "index not READY"),
])
async def test_refused_while_the_queue_jobs_or_indexes_are_not_ready(change, fragment):
    w = world()
    change(w)
    await _refused(w, fragment)


async def test_refused_once_a_connection_has_a_sync_state():
    w = await _applied()
    (conn,) = await w.store.list_for_user(USER)
    w.col(w.names.sync_state)[sync_state_doc_id(conn.scope)] = {"user_id": USER, "connection_id": conn.connection_id}
    await _refused(w, "already has a sync state")


@pytest.mark.parametrize("state", ["working", "waiting"])
async def test_refused_with_a_transcript_in_flight(state):
    w = world()
    w.col(w.names.records)[f"{USER}:item-2"]["transcript_state"] = state
    await _refused(w, "transcript state working/waiting")


async def test_refused_with_an_open_batch_or_reservation():
    w = world()
    w.col(w.names.batches)[f"{USER}:batches%2Fb1"]["state"] = "submitted"
    await _refused(w, "open transcript batches")
    w = world()
    w.col(w.names.usage)[f"{USER}:2026-10"]["transcribe_reserved_usd"] = 0.4
    await _refused(w, "open reservations")


async def test_refused_when_a_mirror_root_or_a_note_is_of_another_drive():
    w = world()
    w.col(w.names.roots)[f"{USER}:drv-other"] = {"user_id": USER, "drive_id": "drv-other", "root_item_id": "r-9"}
    await _refused(w, "mirror root's drive_id differs")
    w = world()
    w.col(w.names.notes)[f"{USER}:n2"] = {"user_id": USER, "note_id": "n2",
                                         "match_keys": [f"i:{ONEDRIVE}:drv-other:item-3", "r:item-3"]}
    await _refused(w, "notes bound to another drive")


async def test_refused_with_two_onedrive_credentials_or_none_with_a_drive_id():
    w = world()
    w.col(w.names.credentials)[f"{USER}:cred-2"] = {**w.col(w.names.credentials)[f"{USER}:{CRED}"],
                                                    "credential_id": "cred-2"}
    await _refused(w, "2 OneDrive credentials")
    w = world()
    w.col(w.names.credentials)[f"{USER}:{CRED}"]["drive_id"] = ""
    await _refused(w, "has no drive_id")


async def test_refused_when_a_placed_archive_folder_cannot_be_re_keyed():
    w = world()
    key = archive_key(DRIVE, DAY, "Calls")
    w.col(w.names.ledger)[key]["source_name"] = "Renamed"     # the chain no longer gives the hashed key
    await _refused(w, "cannot be re-keyed")


# -- the purge order (the old revision finishes a cut purge itself) ------------------------------------------------

async def test_the_purge_flags_are_written_first_under_the_old_lease():
    w = world()
    w.db.collection(w.names.chunks).fail = RuntimeError("store down")
    with pytest.raises(RuntimeError):
        await w.go("apply")
    state = w.doc(w.names.sync_state, USER)
    assert (state["purging"], state["purge_keep_transcripts"], state["lease_owner"]) == (True, True, "cutover-test")
    assert state["lease_until"] > NOW


# -- --purge-new --------------------------------------------------------------------------------------------------

async def test_purge_new_deletes_only_the_connection_scoped_documents():
    w = await _applied()
    n = w.names
    (conn,) = await w.store.list_for_user(USER)
    cid = conn.connection_id
    scoped = {"user_id": USER, "connection_id": cid}
    for name, doc_id in ((n.records, f"{cid}:item-1"), (n.chunks, f"{cid}:item-1:1:0"), (n.hints, f"{cid}:item-1:1:0"),
                         (n.jobs, f"{cid}:item-2"), (n.batches, f"{cid}:batches%2Fb2")):
        w.col(name)[doc_id] = dict(scoped)
    w.db.collection(f"{n.jobs}/{cid}:item-2/{n.windows}").docs["0"] = {"user_id": USER, "connection_id": cid}
    w.col(n.sync_state)[sync_state_doc_id(ConnectionScope(USER, cid))] = dict(scoped)
    kept = {name: dict(w.col(name)) for name in (n.transcripts, n.ledger, n.roots, n.notes, n.usage)}
    code, lines = await w.go("purge-new")
    assert code == 0, lines
    for name in n.index_collections():
        assert not [i for i in w.col(name) if i.startswith(f"{cid}:")], name
    assert w.sub(n.jobs, f"{cid}:item-2", n.windows) == {}
    assert w.doc(n.sync_state, cid) is None
    assert await w.store.list_for_user(USER) == [] and await w.store.get_settings(USER) is None
    for name, docs in kept.items():
        assert w.col(name) == docs, name


async def test_purge_new_refuses_while_the_queue_runs():
    w = await _applied()
    w.infra.paused = False
    code, lines = await w.go("purge-new")
    assert code == 1 and await w.store.list_for_user(USER) != []


# -- --copy-back --------------------------------------------------------------------------------------------------

async def test_copy_back_restores_what_the_old_revision_needs():
    w = await _applied()
    n = w.names
    # after the resume, the new runtime wrote a transcript and re-rendered a ledger file under the new keys
    fresh_key = w.script.new_content_key("QXH2bmV3:250", DRIVE, ONEDRIVE)
    fresh_id = transcript_doc_id(USER, fresh_key)
    w.col(n.transcripts)[fresh_id] = {"user_id": USER, "content_key": fresh_key, "parts": 1, "last_used_at": NOW}
    w.db.collection(f"{n.transcripts}/{fresh_id}/{n.parts}").docs["t2-0"] = {"user_id": USER, "index": 0,
                                                                              "token": "t2"}
    new_file = ledger_key(USER, "file", IDENTITY, onedrive_item_identity(DRIVE, "item-5"))
    w.col(n.ledger)[new_file] = {"user_id": USER, "kind": "file", "drive_identity": IDENTITY,
                                 "source_identity": onedrive_item_identity(DRIVE, "item-5"),
                                 "source_item_id": "item-5", "content_key": fresh_key, "mirror_item_id": "m-5",
                                 "mirror_parent_id": "mf-1", "state": "active"}
    code, lines = await w.go("copy-back")
    assert code == 0, lines
    old_head = w.doc(n.transcripts, f"{USER}:QXH2bmV3:250")
    assert old_head["content_key"] == "QXH2bmV3:250"
    assert w.sub(n.transcripts, f"{USER}:QXH2bmV3:250", n.parts) == {"t2-0": {"user_id": USER, "index": 0,
                                                                                "token": "t2"}}
    old_doc = w.doc(n.ledger, f"{USER}:file:item-5")
    assert (old_doc["drive_id"], old_doc["source_item_id"], old_doc["content_key"]) == (DRIVE, "item-5",
                                                                                        "QXH2bmV3:250")
    assert "drive_identity" not in old_doc
    once = w.snapshot()
    await w.go("copy-back")
    assert w.snapshot() == once          # nothing left to copy back


async def test_copy_back_leaves_an_unchanged_copy_alone():
    w = await _applied()
    before = w.snapshot()
    code, lines = await w.go("copy-back")
    assert code == 0 and w.snapshot() == before, lines


# -- --cleanup ----------------------------------------------------------------------------------------------------

async def test_cleanup_deletes_the_old_documents_that_have_a_twin():
    w = await _applied()
    n = w.names
    (conn,) = await w.store.list_for_user(USER)
    w.col(n.transcripts)[f"{USER}:QXH3:9"] = {"user_id": USER, "content_key": "QXH3:9"}      # no twin: kept
    code, lines = await w.go("cleanup")
    assert code == 0, lines
    assert w.doc(n.transcripts, f"{USER}:{HASH_KEY}") is None
    assert w.sub(n.transcripts, f"{USER}:{HASH_KEY}", n.parts) == {}
    assert w.doc(n.transcripts, f"{USER}:QXH3:9") is not None
    assert w.doc(n.transcripts, transcript_doc_id(USER, _new_key(w, HASH_KEY))) is not None
    assert not [i for i, d in w.col(n.ledger).items() if not d.get("drive_identity")]
    assert w.doc(n.roots, f"{USER}:{DRIVE}") is None and w.doc(n.usage, f"{USER}:initial") is None
    keys = w.doc(n.notes, f"{USER}:n1")["match_keys"]
    assert "r:item-1" not in keys and tagged_ref_key(conn.ref_tag, "item-1") in keys
    assert "drive_id" not in w.doc(n.credentials, f"{USER}:{CRED}")
    assert w.doc(n.users, USER)["config"] == {"tier": "balanced"}


async def test_cleanup_before_apply_deletes_nothing_the_runtime_lacks():
    w = world()
    n = w.names
    heads, ledger = dict(w.col(n.transcripts)), dict(w.col(n.ledger))
    await w.go("cleanup")
    assert w.col(n.transcripts) == heads and w.col(n.ledger) == ledger
    assert "r:item-1" in w.doc(n.notes, f"{USER}:n1")["match_keys"]


async def test_cleanup_dry_run_counts_only():
    w = await _applied()
    before = w.snapshot()
    code, lines = await w.go("cleanup", write=False)
    assert code == 0 and w.snapshot() == before
    assert f"deleted {w.names.transcripts} 3" in lines[-1]


# -- I5 on the copied world: the runtime finds every ledger document where its key function says ----------------

async def test_every_copied_ledger_document_sits_at_the_key_the_runtime_computes():
    w = await _applied()
    for doc_id, doc in w.col(w.names.ledger).items():
        if not doc.get("drive_identity"):
            continue
        if doc["kind"] == "archive":
            assert doc_id == f"{USER}:archive:{doc['source_item_id']}"
        else:
            assert doc_id == ledger_key(USER, doc["kind"], doc["drive_identity"], doc["source_identity"])
