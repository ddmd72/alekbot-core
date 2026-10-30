"""DriveTranscriptMirror: errors never stall the index (USER_DRIVE_INDEXING_RFC §27.9.5, Δ204, Δ216, Δ217; plan Task
21A). Synthetic items only."""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import timedelta

import pytest

from src.domain.drive_index import DriveIndexStoreUnavailable, LeaseLost
from src.domain.drive_index_params import MIRROR_FAILED_RETRY, MIRROR_MAX_ATTEMPTS, MIRROR_QUOTA_HOLD
from src.domain.drive_mirror import LONG_PATHS_NAME, MIRROR_ACTIVE
from src.domain.drive_mirror_plan import mirror_digest
from src.domain.user_drive import (
    DriveAccessDenied, DriveItemNotFoundError, DriveNotConnectedError, DrivePathTooLong, DriveReauthRequired, DriveStorageFull,
    DriveThrottled,
)

from .drive_mirror_fakes import AREA, HANDLE, MirrorWorld

HOUR = timedelta(hours=1)


def _named(name: str):
    return lambda args: args.get("name") == name


async def _ready(w: MirrorWorld) -> None:
    """A root and read-me already in place, so a run's writes are the files'."""
    await w.run()
    w.reset_calls()


async def test_400_charges_the_document_and_the_tick_goes_on():
    w = MirrorWorld()
    await _ready(w)
    a = await w.recording("a.mp4", day="2026-10-02")
    b = await w.recording("b.mp4", day="2026-10-01")
    w.drive.fail("upload", ValueError("400 invalidRequest"), _named("a.mp4.md"))
    counters = await w.run()
    doc, rec = w.doc(a), w.rec(a)
    assert doc.attempts == 1 and doc.retry_at is not None and doc.failed_digest == mirror_digest(rec)
    assert not rec.mirror_due and rec.mirror_retry_at == doc.retry_at and rec.mirror_held_digest == mirror_digest(rec)
    assert w.mirror_path(b) == "b.mp4.md" and counters["mirror_charged"] == 1
    w.advance(doc.retry_at - w.clock)                     # the hold is ripe: woken, then tried again
    await w.run()
    assert w.mirror_path(a) == "a.mp4.md" and w.doc(a).attempts == 0 and not w.rec(a).mirror_retry_at


@pytest.mark.parametrize("exc", [ValueError("423 locked"), ValueError("503 after retries"), OSError("reset"),
                                 TimeoutError(), DriveAccessDenied()])
async def test_423_and_5xx_charged(exc):
    w = MirrorWorld()
    await _ready(w)
    a = await w.recording("a.mp4")
    w.drive.fail("upload", exc)
    await w.run()
    assert w.doc(a).attempts == 1 and not w.rec(a).mirror_due


async def test_parked_after_five():
    w = MirrorWorld()
    await _ready(w)
    a = await w.recording("a.mp4")
    w.drive.fail("upload", ValueError("400"), times=99)
    parked = 0
    for _ in range(MIRROR_MAX_ATTEMPTS):
        counters = await w.run()
        parked += counters.get("mirror_failed", 0)
        doc = w.doc(a)
        if doc.failed_at is None:
            w.advance(doc.retry_at - w.clock)
    doc, rec = w.doc(a), w.rec(a)
    assert doc.attempts == MIRROR_MAX_ATTEMPTS and doc.failed_at == w.clock and parked == 1
    assert rec.mirror_retry_at == w.clock + MIRROR_FAILED_RETRY and not rec.mirror_due
    w.advance(timedelta(days=6))
    w.reset_calls()
    await w.run()
    assert w.drive.writes() == []                         # parked: not tried before the week is over


async def test_parked_retried_on_a_new_digest():
    w = MirrorWorld()
    await _ready(w)
    a = await w.recording("a.mp4")
    w.drive.fail("upload", ValueError("400"), times=MIRROR_MAX_ATTEMPTS)
    for _ in range(MIRROR_MAX_ATTEMPTS):
        await w.run()
        if w.doc(a).failed_at is None:
            w.advance(w.doc(a).retry_at - w.clock)
    assert w.doc(a).failed_at is not None
    await w.update(a, name="a renamed.mp4")               # a new digest ends the hold at once
    assert w.rec(a).mirror_due
    await w.run()
    assert w.mirror_path(a) == "a renamed.mp4.md" and w.doc(a).attempts == 0 and w.doc(a).failed_at is None


async def test_507_holds_the_mirror_and_charges_nothing():
    w = MirrorWorld()
    await _ready(w)
    a = await w.recording("a.mp4", day="2026-10-02")
    b = await w.recording("b.mp4", day="2026-10-01")
    w.drive.fail("upload", DriveStorageFull(), _named("a.mp4.md"))
    counters = await w.run()
    assert counters["mirror_quota_full"] == 1
    assert w.root.quota_hold_until == w.clock + MIRROR_QUOTA_HOLD
    assert w.doc(a).attempts == 0 and w.rec(a).mirror_due and w.rec(a).mirror_retry_at is None
    assert w.mirror_path(b) is None                       # the step ended
    w.reset_calls()
    w.advance(5 * HOUR)
    await w.run()
    assert w.drive.calls == []
    w.advance(2 * HOUR)
    await w.run()
    assert w.mirror_path(a) == "a.mp4.md" and w.mirror_path(b) == "b.mp4.md"


async def test_path_too_long_replaced_in_long_paths_once():
    w = MirrorWorld()
    await _ready(w)
    videos = await w.folder("Videos")
    a = await w.recording("a.mp4", videos)
    w.drive.fail("upload", DrivePathTooLong(), lambda args: args["name"] == "a.mp4.md"
                 and args["parent_id"] != _long_paths(w))
    counters = await w.run()
    assert w.mirror_path(a) == f"{LONG_PATHS_NAME}/a.mp4.md" and counters["mirror_long_path"] == 1
    assert w.doc(a).long_path_source and "Videos" in w.content(a)        # the header keeps the source path
    w.reset_calls()
    await w.update(a, web_url="https://example.invalid/new")             # due again: stays in Long paths
    await w.run()
    assert [c[0] for c in w.drive.writes()] == ["replace_content"]
    b = await w.recording("b.mp4", videos)
    w.drive.fail("upload", DrivePathTooLong(), _named("b.mp4.md"), times=2)   # in Long paths too: charged
    await w.run()
    assert w.doc(b).attempts == 1 and w.doc(b).long_path_source and not w.doc(b).mirror_item_id


def _long_paths(w: MirrorWorld) -> str:
    node = w.drive.child(w.root.root_item_id, LONG_PATHS_NAME) if w.root else None
    return node.item_id if node else "-"


async def test_long_path_counted_from_the_drive_root_goes_to_long_paths():
    w = MirrorWorld(area_path="Apps/" + "A" * 200)
    await _ready(w)
    deep = await w.folder("B" * 120)
    a = await w.recording("C" * 60 + ".mp4", deep)
    await w.run()
    assert w.mirror_path(a) == f"{LONG_PATHS_NAME}/{'C' * 60}.mp4.md"
    assert w.folder_doc(deep) is None                     # no folder chain for a long-path file


@pytest.mark.parametrize("exc", [DriveThrottled(30), DriveNotConnectedError(), DriveReauthRequired()])
async def test_throttle_re_raises(exc):
    w = MirrorWorld()
    await _ready(w)
    a = await w.recording("a.mp4")
    w.drive.fail("upload", exc)
    with pytest.raises(type(exc)):
        await w.run()
    assert w.doc(a).attempts == 0


async def test_store_unavailable_and_lease_lost_re_raise():
    w = MirrorWorld()
    await _ready(w)
    await w.recording("a.mp4")

    async def broken(*a, **kw):
        raise DriveIndexStoreUnavailable("index missing")

    w.repo.mirror_queue = broken                          # type: ignore[assignment]
    with pytest.raises(DriveIndexStoreUnavailable):
        await w.run()
    w2 = MirrorWorld()
    await w2.recording("a.mp4")
    host = w2.host()

    def lost():
        raise LeaseLost()

    host.fence = lost                                     # type: ignore[assignment]
    with pytest.raises(LeaseLost):
        await w2.service.run(HANDLE, host)


async def test_unexpected_error_is_logged_and_charged(caplog):
    w = MirrorWorld()
    await _ready(w)
    a = await w.recording("a.mp4", day="2026-10-02")
    b = await w.recording("b.mp4", day="2026-10-01")
    w.drive.fail("upload", RuntimeError("bug"), _named("a.mp4.md"))
    with caplog.at_level(logging.ERROR):
        counters = await w.run()
    assert counters["mirror_errors"] == 1
    assert [r for r in caplog.records if r.levelno == logging.ERROR and "unexpected RuntimeError" in r.getMessage()]
    # fix round 1 (M3, the controller's ruling on Δ204): charged like any per-action failure, so it parks after the limit
    assert w.doc(a).attempts == 1 and not w.rec(a).mirror_due and w.mirror_path(b) == "b.mp4.md"


async def test_counters_refresh_after_a_failed_action():
    """Service side of the tick test (Task 21C wires it into `_work_off`): a failed action never leaves the step, so the
    run returns its counters normally and the tick's counter refresh after it runs."""
    w = MirrorWorld()
    await _ready(w)
    await w.recording("a.mp4")
    w.drive.fail("upload", ValueError("400"))
    counters = await w.run()
    assert counters["mirror_charged"] == 1 and counters["mirror_ops"] == 1


async def test_failed_archive_holds_the_mark():
    w = MirrorWorld()
    a = await w.recording("a.mp4")
    await w.run()
    await w.delete_source(a)
    w.advance(2 * HOUR)
    w.drive.fail("move", ValueError("423 locked"))
    await w.run()
    doc = w.doc(a)
    assert doc.state == MIRROR_ACTIVE and doc.gone_pending and doc.attempts == 1
    assert doc.mark_ready_at == doc.retry_at and doc.mark_ready_at > w.clock
    assert await w.mirror.ready_marks("user-1", "drive-1", w.clock, 10) == []
    w.advance(doc.retry_at - w.clock)
    await w.run()
    assert w.doc(a).state == "archived"


async def test_no_drive_id_skips_the_step():
    w = MirrorWorld()
    await w.recording("a.mp4", AREA)
    counters = await w.run(drive_identity="")
    assert counters == {"mirror_no_drive_id": 1} and w.drive.calls == []


# -- fix round 1 -----------------------------------------------------------------------------------------------------

async def test_get_root_error_returns_counters():
    w = MirrorWorld()

    async def broken(*a, **kw):
        raise RuntimeError("decode failed")

    w.mirror.get_root = broken                            # type: ignore[assignment]
    counters = await w.run()
    assert counters["mirror_errors"] == 1


async def test_create_folder_error_returns_counters():
    w = MirrorWorld()
    await w.recording("a.mp4")
    w.drive.fail("create_folder", RuntimeError("unmapped adapter error"))
    counters = await w.run()
    assert counters["mirror_errors"] == 1 and w.root is None


@pytest.mark.parametrize("read", ["ready_marks", "mirror_queue", "seed"])
async def test_corrupt_doc_decode_returns_counters(read):
    w = MirrorWorld()
    await _ready(w)
    await w.recording("a.mp4")

    async def corrupt(*a, **kw):
        raise ValueError("a stored field does not decode")

    if read == "ready_marks":
        w.mirror.ready_marks = corrupt                    # type: ignore[assignment]
    elif read == "mirror_queue":
        w.repo.mirror_queue = corrupt                     # type: ignore[assignment]
    else:
        await w.mirror.save_root(replace(w.root, seed_version=""))
        w.repo.transcript_records = corrupt               # type: ignore[assignment]
    counters = await w.run()
    assert counters["mirror_errors"] == 1


async def test_unexpected_error_parks_and_frees_the_queue_head(monkeypatch):
    from src.services import drive_transcript_mirror as mod
    monkeypatch.setattr(mod, "QUEUE_PAGE", 1)             # one bad record fills the whole page
    w = MirrorWorld()
    await _ready(w)
    bad = await w.recording("bad.mp4", day="2026-10-05")
    good = await w.recording("good.mp4", day="2026-10-01")
    w.drive.fail("upload", RuntimeError("render bug"), _named("bad.mp4.md"), times=99)
    await w.run()
    assert w.mirror_path(good) == "good.mp4.md"           # the head no longer blocks the older record
    for _ in range(MIRROR_MAX_ATTEMPTS):
        doc = w.doc(bad)
        if doc.failed_at is not None:
            break
        w.advance(doc.retry_at - w.clock)
        await w.run()
    assert w.doc(bad).failed_at is not None and w.doc(bad).attempts == MIRROR_MAX_ATTEMPTS
    w.reset_calls()
    w.advance(HOUR)
    await w.run()
    assert w.drive.writes() == []                         # parked: no longer retried


async def test_log_lines_carry_no_path_or_name(caplog):
    w = MirrorWorld()
    await _ready(w)
    a = await w.recording("Secret Interview.mp4")
    w.drive.fail("upload", DriveItemNotFoundError("Drive item not found: /me/drive/items/p1:/Secret Interview.mp4.md:"
                                                  "/content"))
    w.drive.fail("get_change", RuntimeError("while reading /drive/root:/Apps/Alek/Secret Interview.mp4"))
    with caplog.at_level(logging.DEBUG):
        counters = await w.run()
    assert counters["mirror_errors"] == 1
    assert "Secret" not in caplog.text and "/me/drive" not in caplog.text
    assert all(r.exc_info is None for r in caplog.records)
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert errors and "message digest" in errors[0] and "drive_transcript_mirror.py" in errors[0]   # frames kept
    assert f"due file {_hid(a)}" in errors[0] and a not in errors[0].split("\n")[0]


def _hid(item_id: str) -> str:
    import hashlib
    return hashlib.sha256(item_id.encode("utf-8")).hexdigest()[:10]
