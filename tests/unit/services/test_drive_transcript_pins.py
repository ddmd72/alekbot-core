"""Pins (USER_DRIVE_INDEXING_RFC §27.9.6, §27.9.12; plan Task 20B; Δ185, Δ186, Δ214, Δ217): a store entry a ledger
document references while `archived`, `detached` or carrying `gone_pending` — on any drive of the user — survives the
daily orphan sweep (kept and touched) and every purge (the scanning delete, the sweep in its keep mode). The purge reads
the pins and never writes the mirror collections; it deletes nothing on the drive (OQ-T10). Synthetic data only."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, List

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.adapters.in_memory_drive_mirror_repo import InMemoryDriveMirrorRepository
from src.domain.drive_index_params import ACCESS_LOSS_TTL, RECORD_WRITE_BATCH, TRANSCRIPT_ORPHAN_TTL
from src.domain.drive_mirror import (
    GONE, KIND_FILE, MIRROR_ACTIVE, MIRROR_ARCHIVED, MIRROR_DETACHED, DeletionNotice, MirrorDoc,
)
from src.domain.drive_transcript import Segment, StitchedTranscript, StoredTranscript
from src.domain.user_drive import DriveConnection
from src.services.drive_transcript_stage import DriveTranscriptStage

from .drive_content_world import admitted, prose  # noqa: F401  (fixture)
from .drive_mirror_tick_world import DRIVE_IDENTITY, MirrorWorld, source_identity
from .drive_transcript_fakes import (
    HANDLE, NOW, USER, Clock, FakeBatch, FakeDrive, FakeHost, FakeMedia, FakeRunner,
)

pytestmark = pytest.mark.usefixtures("admitted")

TEXT = StitchedTranscript((Segment(0.0, None, "first words"), Segment(30.0, None, "second words")), ("en",), 1, 60.0)


def ledger(content_key: str, *, state: str = MIRROR_ARCHIVED, drive: str = DRIVE_IDENTITY, item: str = "",
           user: str = "u", **kw: Any) -> MirrorDoc:
    source = item or f"src-{content_key}"
    return MirrorDoc(user_id=user, drive_identity=drive, kind=KIND_FILE, source_item_id=source,
                     source_identity=source_identity(source, drive), content_key=content_key,
                     mirror_item_id=f"mf-{content_key}", state=state, **kw)


# -- the orphan sweep (§27.8 b, Δ186) -----------------------------------------------------------------------------------

def _stage_env(tmp_path, *, mirror: bool = True):
    clock = Clock()
    repo = InMemoryDriveIndexRepository()
    ledger_repo = InMemoryDriveMirrorRepository()
    calls: List[str] = []
    real_pinned = ledger_repo.pinned

    async def pinned(user_id: str, key: str) -> bool:
        calls.append(key)
        return await real_pinned(user_id, key)
    ledger_repo.pinned = pinned
    stage = DriveTranscriptStage(repo, FakeDrive(), FakeMedia(clock), FakeBatch(clock), FakeRunner(), clock=clock,
                                 monotonic=clock.monotonic, temp_root=str(tmp_path),
                                 mirror=ledger_repo if mirror else None)
    return SimpleNamespace(clock=clock, repo=repo, mirror=ledger_repo, stage=stage, host=FakeHost(repo, clock),
                           pinned_calls=calls)


async def _sweep(env):
    return await env.stage.cheap_steps(HANDLE, env.host, stop_at=env.clock.mono + 600)


async def test_orphan_sweep_keeps_and_touches_a_pinned_key(tmp_path):
    env = _stage_env(tmp_path)
    long_ago = NOW - TRANSCRIPT_ORPHAN_TTL - timedelta(days=1)
    for key in ("archived", "detached", "orphan", "active"):
        await env.repo.put_transcript(USER, StoredTranscript(key, TEXT, last_used_at=long_ago))
    await env.mirror.save_doc(ledger("archived", user=USER))
    await env.mirror.save_doc(ledger("detached", user=USER, state=MIRROR_DETACHED, drive="another-drive"))
    await env.mirror.save_doc(ledger("active", user=USER, state=MIRROR_ACTIVE))   # an active file pins nothing
    summary = await _sweep(env)
    for key in ("archived", "detached"):
        kept = env.repo.transcripts[(USER, key)]
        assert kept.last_used_at == env.clock.now and kept.transcript == TEXT   # touched: off the sweep page's front
    assert (USER, "orphan") not in env.repo.transcripts and (USER, "active") not in env.repo.transcripts
    assert summary["transcript_orphans_kept"] == 2 and summary["transcript_orphans_deleted"] == 2


async def test_pin_holds_during_the_grace_hour(tmp_path):
    """A source just deleted: its document is still `active` with `gone_pending` (inside MIRROR_ARCHIVE_GRACE): pinned."""
    env = _stage_env(tmp_path)
    long_ago = NOW - TRANSCRIPT_ORPHAN_TTL - timedelta(days=1)
    await env.repo.put_transcript(USER, StoredTranscript("marked", TEXT, last_used_at=long_ago))
    await env.mirror.save_doc(ledger("marked", user=USER, state=MIRROR_ACTIVE, item="a"))
    await env.mirror.mark_gone(USER, [DeletionNotice("a", GONE, NOW, source_identity=source_identity("a"))],
                               drive_identity=DRIVE_IDENTITY)
    await _sweep(env)
    assert env.repo.transcripts[(USER, "marked")].last_used_at == env.clock.now


async def test_a_key_still_in_use_needs_no_pin_read(tmp_path):
    env = _stage_env(tmp_path)
    long_ago = NOW - TRANSCRIPT_ORPHAN_TTL - timedelta(days=1)
    await env.repo.put_transcript(USER, StoredTranscript("in-use", TEXT, last_used_at=long_ago))
    from tests.unit.services.drive_transcript_fakes import record
    await env.repo.upsert_records([replace(record("c", content_hash="in-use"), transcript_state="current",
                                           transcript_key="in-use")])
    await _sweep(env)
    assert env.pinned_calls == [] and (USER, "in-use") in env.repo.transcripts


async def test_orphan_sweep_without_a_mirror_is_unchanged(tmp_path):
    env = _stage_env(tmp_path, mirror=False)
    long_ago = NOW - TRANSCRIPT_ORPHAN_TTL - timedelta(days=1)
    await env.repo.put_transcript(USER, StoredTranscript("archived", TEXT, last_used_at=long_ago))
    await env.mirror.save_doc(ledger("archived", user=USER))
    await _sweep(env)
    assert (USER, "archived") not in env.repo.transcripts and env.pinned_calls == []


# -- the purges (§27.9.12, Δ214) -----------------------------------------------------------------------------------------

@pytest.fixture
def world(tmp_path):
    def make(**kw: Any) -> MirrorWorld:
        return MirrorWorld(tmp_path, **kw)
    return make


def record_store_calls(w: MirrorWorld) -> List[Any]:
    calls: List[Any] = []
    for name in ("delete_user_transcripts", "delete_user_transcripts_except", "delete_user_transcript_orphans"):
        real = getattr(w.repo, name)

        async def spy(*a: Any, _real: Any = real, _name: str = name, **kw: Any) -> Any:
            out = await _real(*a, **kw)
            calls.append((_name, a, kw, out))
            return out
        setattr(w.repo, name, spy)
    return calls


async def _onboarded_with_store(world, pinned: List[str], unpinned: List[str], *, pin_drive: str = DRIVE_IDENTITY, **kw):
    w = world(**kw)
    w.add_root()
    w.add_doc("notes.md", prose(4))
    await w.settle()
    for key in pinned + unpinned:
        await w.repo.put_transcript("u", StoredTranscript(key, TEXT, last_used_at=w.clock.now))
    for key in pinned:
        w.mirror.docs[ledger(key, drive=pin_drive).key] = ledger(key, drive=pin_drive)
    return w


def _store(w: MirrorWorld) -> List[str]:
    return sorted(ck for (u, ck) in w.repo.transcripts if u == "u")


async def test_purge_keeps_pinned_store_and_their_parts(world):
    w = await _onboarded_with_store(world, ["pin-a", "pin-b"], ["loose-a", "loose-b"])
    calls = record_store_calls(w)
    w.users.indexing = False
    await w.settle()   # the opt-out purge
    assert w.repo.states.get("u") is None
    assert _store(w) == ["pin-a", "pin-b"]
    assert all(w.repo.transcripts[("u", k)].transcript == TEXT for k in ("pin-a", "pin-b"))   # the segments too
    sweeps = [c for c in calls if c[0] == "delete_user_transcript_orphans"]
    assert sweeps and all(c[2]["keep_transcripts"] is True for c in sweeps)   # windows only: the parts survive
    assert not any(c[0] == "delete_user_transcripts" for c in calls)


async def test_purge_deletes_every_unpinned_with_many_pins(world):
    """More pinned heads than one page, all before the deletable ones (doc ids sort `a…` < `z…`): every unpinned head is
    deleted, the scan is called until its cursor is None."""
    pinned = [f"a{i:04d}" for i in range(RECORD_WRITE_BATCH + 30)]
    unpinned = [f"z{i:04d}" for i in range(25)]
    w = await _onboarded_with_store(world, pinned, unpinned)
    calls = record_store_calls(w)
    w.users.indexing = False
    await w.settle()
    assert w.repo.states.get("u") is None
    assert _store(w) == sorted(pinned)
    scans = [c for c in calls if c[0] == "delete_user_transcripts_except"]
    assert len(scans) >= 2 and scans[0][3] == (0, scans[0][3][1]) and scans[0][3][1] is not None   # a page of pins
    assert scans[-1][3][1] is None
    assert all(c[1][2] == frozenset(pinned) for c in scans)


async def test_access_loss_purge_keeps_a_pinned_entry(world):
    """No drive connected: the pins are per user, not per drive (Δ217), so the purge after ACCESS_LOSS_TTL keeps it."""
    w = await _onboarded_with_store(world, ["pinned"], ["loose"])
    w.drive.connection_state = DriveConnection(connected=False)
    await w.tick()
    w.clock.advance(ACCESS_LOSS_TTL.total_seconds() + 60)
    await w.settle()
    assert w.repo.states.get("u") is None
    assert _store(w) == ["pinned"]


async def test_drive_switch_purge_keeps_a_pinned_entry(world):
    """Another drive connected: the old drive's archived document still pins its entry."""
    w = await _onboarded_with_store(world, ["pinned"], ["loose"])
    w.drive.connection_state = DriveConnection(connected=True, drive_id="d2")
    for _ in range(10):
        result = await w.tick()
        if result.status == "purged":
            break
    assert result.status == "purged"
    assert _store(w) == ["pinned"]


async def test_purge_never_writes_the_ledger(world):
    w = await _onboarded_with_store(world, ["pinned"], ["loose"])
    roots, docs = dict(w.mirror.root_docs), dict(w.mirror.docs)
    items = dict(w.drive.items)
    log = len(w.drive.log)
    start = len(w.mirror.calls)
    w.users.indexing = False
    await w.settle()
    assert w.repo.states.get("u") is None
    assert [c for c in w.mirror.calls[start:] if c != "pinned_keys"] == []
    assert w.mirror.calls[start:].count("pinned_keys") >= 1
    assert w.mirror.root_docs == roots and w.mirror.docs == docs
    assert w.drive.items == items and len(w.drive.log) == log   # nothing on the drive either (OQ-T10)


async def test_purge_without_pins_is_the_plain_delete(world):
    w = await _onboarded_with_store(world, [], ["loose-a", "loose-b"])
    calls = record_store_calls(w)
    w.users.indexing = False
    await w.settle()
    assert _store(w) == []
    assert any(c[0] == "delete_user_transcripts" for c in calls)
    assert not any(c[0] == "delete_user_transcripts_except" for c in calls)
    assert all(c[2]["keep_transcripts"] is False for c in calls if c[0] == "delete_user_transcript_orphans")


async def test_rebuild_reads_no_pins(world):
    """A rebuild keeps the whole store (§27.8 b): no pin read, nothing of the store deleted."""
    w = await _onboarded_with_store(world, ["pinned"], ["loose"])
    start = len(w.mirror.calls)
    w.repo.states["u"] = replace(w.repo.states["u"], purging=True, purge_keep_transcripts=True)
    await w.settle()
    assert "pinned_keys" not in w.mirror.calls[start:]
    assert _store(w) == ["loose", "pinned"]


async def test_no_mirror_repository_purges_as_before(world):
    w = await _onboarded_with_store(world, ["pinned"], ["loose"], wired=False)
    w.users.indexing = False
    await w.settle()
    assert _store(w) == [] and w.mirror.calls == []
