"""The orphan-document sweep and the transcript mirror (USER_DRIVE_INDEXING_RFC §27.9.12, Δ186, Δ214, Δ217): the default keeps
the root documents, the ledger and the pinned store entries; `--mirror` deletes all three. In-memory repositories only: the
sweep never touches a drive (it has no drive port)."""
import importlib.util
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.adapters.in_memory_drive_mirror_repo import InMemoryDriveMirrorRepository
from src.domain.drive_mirror import (
    KIND_ARCHIVE, KIND_FILE, KIND_FOLDER, MIRROR_ACTIVE, MIRROR_ARCHIVED, MIRROR_DETACHED, MirrorDoc, MirrorRoot,
)
from src.domain.drive_enrichment import ModelUsageMonth
from src.domain.drive_index import DriveIndexStoreUnavailable
from src.domain.drive_transcript import StoredTranscript

from src.domain.connection_scope import ConnectionScope

from tests.unit.services.drive_index_fakes import T0

SCOPE = ConnectionScope("u", "u-c")   # the connection the fixtures belong to (MULTI_DRIVE_RFC §8)
_DIR = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sweep():
    return _load("drive_transcript_sweep")


async def _store(repo, key):
    await repo.put_transcript("u", StoredTranscript(content_key=key, transcript=None, stub="", transcribe_version=1))


def _doc(item, kind=KIND_FILE, state=MIRROR_ACTIVE, key="", drive="d1", **kw):
    return MirrorDoc(user_id="u", drive_identity=drive, kind=kind, source_item_id=item, source_identity=f"{drive}:{item}",
                     state=state, content_key=key, **kw)


async def _world(keys=("a", "b", "c"), pinned=("a",)):
    """Stored transcripts `keys`; the ledger pins `pinned` through archived documents, plus an active document and a
    folder document that pin nothing; one root document."""
    index, mirror = InMemoryDriveIndexRepository(), InMemoryDriveMirrorRepository()
    for key in keys:
        await _store(index, key)
    await mirror.save_root(MirrorRoot(user_id="u", drive_identity="d1", root_item_id="r1", root_name="_Transcripts"))
    for i, key in enumerate(pinned):
        await mirror.save_doc(_doc(f"arch{i}", state=MIRROR_ARCHIVED, key=key))
    await mirror.save_doc(_doc("live", key="b"))
    await mirror.save_doc(_doc("fold", kind=KIND_FOLDER))
    await mirror.save_doc(_doc("day", kind=KIND_ARCHIVE))
    return index, mirror


def _deleter(mirror):
    async def delete(user_id, drive_identity):
        mirror.root_docs.pop((user_id, drive_identity), None)
    return delete


def _kept(index):
    return sorted(ck for (u, ck) in index.transcripts if u == "u")


async def test_default_keeps_pinned_store_entries_ledger_and_roots(sweep):
    index, mirror = await _world()
    code, message = await sweep.sweep_user(index, "u", scopes=[SCOPE], apply=True, now=T0, owner="s", after_rollback=True,
                                           mirror_repo=mirror, root_deleter=_deleter(mirror))
    assert code == 0 and _kept(index) == ["a"]            # b and c deleted, the pinned a stays
    assert len(mirror.docs) == 4 and len(mirror.root_docs) == 1
    assert "kept 1 pinned" in message


async def test_default_with_many_pins_pages_past_a_page_of_pinned_heads(sweep, monkeypatch):
    monkeypatch.setattr(sweep, "_BATCH", 3)
    keys = [f"k{i:02d}" for i in range(12)]
    pinned = keys[:8]            # more pinned heads than one page (Δ214): a page of only kept heads is not the end
    index, mirror = await _world(keys=keys, pinned=pinned)
    code, _ = await sweep.sweep_user(index, "u", scopes=[SCOPE], apply=True, now=T0, owner="s", after_rollback=True,
                                     mirror_repo=mirror, root_deleter=_deleter(mirror))
    assert code == 0 and _kept(index) == sorted(pinned)


async def test_default_runs_the_orphan_sweep_in_keep_mode_while_pins_exist(sweep):
    index, mirror = await _world()
    seen = []
    original = index.delete_user_transcript_orphans

    async def spy(user_id, limit, *, keep_transcripts):
        seen.append(keep_transcripts)
        return await original(user_id, limit, keep_transcripts=keep_transcripts)
    index.delete_user_transcript_orphans = spy
    await sweep.sweep_user(index, "u", scopes=[SCOPE], apply=True, now=T0, owner="s", after_rollback=True, mirror_repo=mirror,
                           root_deleter=_deleter(mirror))
    assert seen and set(seen) == {True}


async def test_default_without_pins_deletes_every_stored_transcript(sweep):
    index, mirror = await _world(pinned=())
    await sweep.sweep_user(index, "u", scopes=[SCOPE], apply=True, now=T0, owner="s", after_rollback=True, mirror_repo=mirror,
                           root_deleter=_deleter(mirror))
    assert _kept(index) == [] and len(mirror.docs) == 3 and len(mirror.root_docs) == 1


async def test_detached_and_marked_documents_pin_too(sweep):
    index, mirror = await _world(keys=("a", "b", "c"), pinned=())
    await mirror.save_doc(_doc("det", state=MIRROR_DETACHED, key="b"))
    await mirror.save_doc(_doc("mark", key="c", gone_pending=True))
    await sweep.sweep_user(index, "u", scopes=[SCOPE], apply=True, now=T0, owner="s", after_rollback=True, mirror_repo=mirror,
                           root_deleter=_deleter(mirror))
    assert _kept(index) == ["b", "c"]


async def test_mirror_flag_deletes_store_ledger_and_roots(sweep):
    index, mirror = await _world()
    code, message = await sweep.sweep_user(index, "u", scopes=[SCOPE], apply=True, now=T0, owner="s", after_rollback=True,
                                           mirror_repo=mirror, mirror=True, root_deleter=_deleter(mirror))
    assert code == 0 and _kept(index) == [] and not mirror.docs and not mirror.root_docs
    assert "4 ledger documents, 1 root documents" in message


async def test_mirror_flag_catches_archived_documents_of_a_drive_without_a_root(sweep):
    index, mirror = await _world()
    await mirror.save_doc(_doc("lost", state=MIRROR_ARCHIVED, key="c", drive="gone-drive"))
    await sweep.sweep_user(index, "u", scopes=[SCOPE], apply=True, now=T0, owner="s", after_rollback=True, mirror_repo=mirror,
                           mirror=True, root_deleter=_deleter(mirror))
    assert not mirror.docs


async def test_mirror_flag_leaves_other_users_alone(sweep):
    index, mirror = await _world()
    await mirror.save_root(MirrorRoot(user_id="w", drive_identity="dw", root_item_id="rw", root_name="_Transcripts"))
    await mirror.save_doc(MirrorDoc(user_id="w", drive_identity="dw", kind=KIND_FILE, source_item_id="x", source_identity="dw:x", content_key="a"))
    await sweep.sweep_user(index, "u", scopes=[SCOPE], apply=True, now=T0, owner="s", after_rollback=True, mirror_repo=mirror,
                           mirror=True, root_deleter=_deleter(mirror))
    assert [d.user_id for d in mirror.docs.values()] == ["w"] and ("w", "dw") in mirror.root_docs


async def test_dry_run_writes_nothing_and_says_what_it_keeps_or_deletes(sweep):
    index, mirror = await _world()
    code, kept = await sweep.sweep_user(index, "u", scopes=[SCOPE], apply=False, now=T0, owner="s", after_rollback=True,
                                        mirror_repo=mirror, root_deleter=_deleter(mirror))
    code2, gone = await sweep.sweep_user(index, "u", scopes=[SCOPE], apply=False, now=T0, owner="s", after_rollback=True,
                                         mirror_repo=mirror, mirror=True, root_deleter=_deleter(mirror))
    assert code == code2 == 0 and "keeping 1 pinned" in kept and "would delete 4 ledger documents and 1 root" in gone
    assert _kept(index) == ["a", "b", "c"] and len(mirror.docs) == 4 and len(mirror.root_docs) == 1


async def test_mirror_flag_without_a_deleter_is_refused(sweep):
    index, mirror = await _world()
    code, message = await sweep.sweep_user(index, "u", scopes=[SCOPE], apply=True, now=T0, owner="s", mirror_repo=mirror, mirror=True)
    assert code == 1 and "--mirror" in message and len(mirror.docs) == 4 and _kept(index) == ["a", "b", "c"]


def test_the_sweep_has_no_drive_port():
    src = (_DIR / "drive_transcript_sweep.py").read_text()
    assert "UserDrivePort" not in src and "user_drive" not in src


async def test_the_module_defaults_to_a_dry_run(sweep):
    src = (_DIR / "drive_transcript_sweep.py").read_text()
    assert 'parser.add_argument("--apply", action="store_true"' in src and "--mirror" in src


async def test_mirror_apply_is_refused_without_after_rollback(sweep):
    index, mirror = await _world()
    code, message = await sweep.sweep_user(index, "u", scopes=[SCOPE], apply=True, now=T0, owner="s", mirror_repo=mirror, mirror=True,
                                           root_deleter=_deleter(mirror))
    assert code == 1 and "--after-rollback" in message and "_Transcripts" in message
    assert _kept(index) == ["a", "b", "c"] and len(mirror.docs) == 4 and len(mirror.root_docs) == 1


async def test_mirror_dry_run_needs_no_after_rollback(sweep):
    index, mirror = await _world()
    code, message = await sweep.sweep_user(index, "u", scopes=[SCOPE], apply=False, now=T0, owner="s", mirror_repo=mirror, mirror=True,
                                           root_deleter=_deleter(mirror))
    assert code == 0 and "would delete 4 ledger documents" in message


async def test_mirror_apply_with_after_rollback_runs(sweep):
    index, mirror = await _world()
    code, _ = await sweep.sweep_user(index, "u", scopes=[SCOPE], apply=True, now=T0, owner="s", after_rollback=True, mirror_repo=mirror,
                                     mirror=True, root_deleter=_deleter(mirror))
    assert code == 0 and not mirror.docs and not mirror.root_docs and _kept(index) == []


def test_help_and_docstring_state_the_precondition(sweep):
    assert "BEFORE any T2 revision" in sweep.__doc__
    src = (_DIR / "drive_transcript_sweep.py").read_text()
    assert "--mirror --apply needs --after-rollback" in src


async def test_pin_indexes_missing_exits_1_before_anything_is_settled(sweep):
    index, mirror = await _world()
    index.usage[("u", T0.strftime("%Y-%m"))] = ModelUsageMonth(transcribe_reserved_usd=1.5, transcribe_usd=2.0)

    async def broken(user_id):
        raise DriveIndexStoreUnavailable("index not ready")
    mirror.pinned_keys = broken
    for apply in (False, True):
        code, message = await sweep.sweep_user(index, "u", scopes=[SCOPE], apply=apply, now=T0, owner="s", after_rollback=True,
                                               mirror_repo=mirror, root_deleter=_deleter(mirror))
        assert code == 1 and "indexes" in message and "not READY" in message
    assert index.usage[("u", T0.strftime("%Y-%m"))].transcribe_reserved_usd == 1.5 and _kept(index) == ["a", "b", "c"]


async def test_firestore_root_deleter_deletes_the_roots_document(sweep):
    from src.domain.drive_mirror import mirror_root_doc_id
    db = MagicMock()
    ref = db.collection.return_value.document.return_value
    ref.delete = AsyncMock()
    env = MagicMock(drive_mirror_roots_collection="pfx_drive_mirror_roots")
    await sweep.firestore_root_deleter(db, env)("u", "d1")
    db.collection.assert_called_once_with("pfx_drive_mirror_roots")
    db.collection.return_value.document.assert_called_once_with(mirror_root_doc_id("u", "d1"))
    ref.delete.assert_awaited_once()
