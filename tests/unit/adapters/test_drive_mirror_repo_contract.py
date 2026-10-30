"""DriveMirrorRepository: one case list, run against the in-memory reference and the Firestore adapter on the
evaluating fake (USER_DRIVE_INDEXING_RFC §27.9.2-§27.9.4, §27.9.11, Δ199, Δ205, Δ206, Δ217)."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from google.api_core.exceptions import FailedPrecondition, ServiceUnavailable

from src.adapters.firestore_drive_mirror_repo import FirestoreDriveMirrorRepository, doc_from_doc, doc_to_doc
from src.adapters.in_memory_drive_mirror_repo import InMemoryDriveMirrorRepository
from src.domain.drive_index import DriveIndexStoreUnavailable
from src.domain.drive_index_params import MIRROR_ARCHIVE_GRACE
from src.domain.drive_mirror import (
    GONE, KIND_ARCHIVE, KIND_FILE, KIND_FOLDER, MIRROR_ACTIVE, MIRROR_ARCHIVED, MIRROR_DETACHED, UNKNOWN, UNSTORED,
    DeletionNotice, MirrorDoc, MirrorRoot,
)
from src.ports.drive_mirror_repository import ARCHIVE_FAMILY, NAME_FAMILY

from tests.unit.adapters.firestore_drive_eval_fakes import eval_transactions
from tests.unit.adapters.firestore_drive_mirror_fakes import LEDGER, ROOTS, NameAwareDb

T = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)
U, D1, D2 = "u", "microsoft_onedrive:drive1", "microsoft_onedrive:drive2"   # drive identities (MULTI_DRIVE_RFC §7.2)


@pytest.fixture(params=["memory", "firestore"])
def adapter(request):
    if request.param == "memory":
        yield InMemoryDriveMirrorRepository(), None
        return
    env = MagicMock()
    env.drive_mirror_roots_collection = ROOTS
    env.drive_transcript_mirror_collection = LEDGER
    db = NameAwareDb()
    with eval_transactions():
        yield FirestoreDriveMirrorRepository(db, env), db


@pytest.fixture
def repo(adapter):
    return adapter[0]


def sid(item, drive=D1) -> str:
    """The source item's adapter-built identity on `drive` (MULTI_DRIVE_RFC §7.2)."""
    return f"{drive}:{item}"


def doc(item="a", kind=KIND_FILE, drive=D1, user=U, **kw) -> MirrorDoc:
    return MirrorDoc(user_id=user, drive_identity=drive, kind=kind, source_item_id=item, source_identity=sid(item, drive),
                     **kw)


def notice(item, why, at, drive=D1) -> DeletionNotice:
    return DeletionNotice(item, why, at, source_identity=sid(item, drive))


def root(drive=D1, user=U, item="r1", **kw) -> MirrorRoot:
    return MirrorRoot(user_id=user, drive_identity=drive, root_item_id=item, root_name="_Transcripts", **kw)


async def save(repo, *docs):
    for d in docs:
        await repo.save_doc(d)


def keys(docs):
    return [d.key for d in docs]


# -- roots ------------------------------------------------------------------------------------------------------------

async def test_roots_lists_every_drive(repo):
    await repo.save_root(root(D2, item="r2"))
    await repo.save_root(root(D1))
    await repo.save_root(root(D1, user="other"))
    assert [r.drive_identity for r in await repo.roots(U)] == [D1, D2]
    assert await repo.roots("nobody") == []


async def test_root_roundtrip_per_drive(repo):
    full = root(D1, readme_item_id="rd", readme_ctag="ct", previous_root_ids=("old1", "old2"), created_at=T,
                root_gone_at=T + timedelta(hours=1), seed_version="3.2", seed_cursor="c1", scan_cursor="c2",
                audited_at=T, audit_folder_key="k", audit_page_cursor="p", orphans_reported=True,
                quota_hold_until=T + timedelta(hours=6))
    await repo.save_root(full)
    await repo.save_root(root(D2, item="r2"))
    assert await repo.get_root(U, D1) == full
    assert (await repo.get_root(U, D2)).root_item_id == "r2"
    assert await repo.get_root(U, "none") is None
    await repo.save_root(replace(full, seed_cursor="c9"))
    assert (await repo.get_root(U, D1)).seed_cursor == "c9"


async def test_root_gone_mark_and_clear(repo):
    await repo.save_root(root(D1, previous_root_ids=("old",)))
    assert await repo.mark_root_gone(U, "r1", T) is True
    assert (await repo.get_root(U, D1)).root_gone_at == T
    await repo.mark_root_gone(U, "r1", T + timedelta(hours=3))
    assert (await repo.get_root(U, D1)).root_gone_at == T          # the earlier mark stays
    assert await repo.mark_root_gone(U, "old", T) is False          # a previous root triggers nothing
    assert await repo.mark_root_gone(U, "missing", T) is False
    assert await repo.clear_root_gone(U, "r1") is True
    assert (await repo.get_root(U, D1)).root_gone_at is None
    assert await repo.clear_root_gone(U, "missing") is False


# -- ledger documents -------------------------------------------------------------------------------------------------

@pytest.mark.slow
async def test_doc_roundtrip_carries_every_field(adapter):
    repo, _ = adapter
    full = MirrorDoc(
        user_id=U, drive_identity=D1, kind=KIND_FILE, source_item_id="a", source_identity=sid("a"), source_name="n", source_parent_id="sp",
        content_key="ck", mirror_item_id="mi", mirror_parent_id="mp", mirror_name="mn", mirror_ctag="ct",
        rendered_hash="h", render_version=3, render_language="uk", state=MIRROR_ARCHIVED, source_path="/x/y",
        seconds=12.5, speakers=2, languages=("uk", "en"), gone_pending=True, gone_why=GONE, gone_at=T,
        mark_ready_at=T + timedelta(hours=1), pending_op="move", pending_name="pn", pending_parent_id="pp",
        pending_at=T, attempts=2, failed_digest="fd", retry_at=T, failed_at=T, checked_at=T, archived_at=T,
        archive_day="2026-10-21", archive_keys=("w:a", "w:b"), name_keys=("w:n",))
    await repo.save_doc(full)
    assert (await repo.get_docs(U, [full.key])) == {full.key: full}
    assert doc_from_doc(doc_to_doc(full)) == full


async def test_get_delete_and_other_users(repo):
    a, b = doc("a"), doc("b")
    await save(repo, a, b, doc("a", user="other"))
    assert keys((await repo.get_docs(U, [a.key, b.key, "u:file:none", "other:file:a"])).values()) == [a.key, b.key]
    await repo.delete_doc(U, "other:file:a")      # another user's key is never touched
    assert (await repo.get_docs("other", ["other:file:a"]))
    await repo.delete_doc(U, a.key)
    await repo.delete_doc(U, a.key)               # missing is a no-op
    assert list(await repo.get_docs(U, [a.key, b.key])) == [b.key]


async def test_by_mirror_item(repo):
    await save(repo, doc("a", mirror_item_id="m1"), doc("b", mirror_item_id="m2"), doc("c", user="o", mirror_item_id="m1"))
    assert (await repo.by_mirror_item(U, "m2")).source_item_id == "b"
    assert (await repo.by_mirror_item(U, "m1")).user_id == U
    assert await repo.by_mirror_item(U, "none") is None


# -- marks --------------------------------------------------------------------------------------------------------------

async def test_mark_gone_updates_existing_only(repo):
    await save(repo, doc("a", source_name="keep"), doc("f1", kind=KIND_FOLDER), doc("x", user="o"))
    count = await repo.mark_gone(U, [notice("a", GONE, T), notice("f1", UNSTORED, T),
                                     notice("never-mirrored", GONE, T), notice("x", GONE, T)],
                                drive_identity=D1)
    assert count == 2
    got = await repo.get_docs(U, ["u:file:a", "u:folder:f1", "u:file:never-mirrored", "u:folder:never-mirrored"])
    assert sorted(got) == ["u:file:a", "u:folder:f1"]                 # nothing was created
    a = got["u:file:a"]
    assert (a.gone_pending, a.gone_why, a.gone_at, a.mark_ready_at, a.source_name) == (
        True, GONE, T, T + MIRROR_ARCHIVE_GRACE, "keep")
    assert got["u:folder:f1"].gone_why == UNSTORED
    assert not (await repo.get_docs("o", ["o:file:x"]))["o:file:x"].gone_pending


async def test_mark_gone_merges_a_repeated_notice(repo):
    await save(repo, doc("a"))
    await repo.mark_gone(U, [notice("a", UNKNOWN, T)], drive_identity=D1)
    await repo.mark_gone(U, [notice("a", GONE, T + timedelta(hours=2))], drive_identity=D1)
    a = (await repo.get_docs(U, ["u:file:a"]))["u:file:a"]
    assert (a.gone_why, a.gone_at, a.mark_ready_at) == (GONE, T, T + timedelta(hours=2) + MIRROR_ARCHIVE_GRACE)


async def test_clear_mark(repo):
    await save(repo, doc("a"), doc("b"))
    await repo.mark_gone(U, [notice("a", GONE, T), notice("b", GONE, T)], drive_identity=D1)
    await repo.clear_mark(U, "u:file:a")
    await repo.clear_mark(U, "u:file:none")       # missing is a no-op, nothing created
    got = await repo.get_docs(U, ["u:file:a", "u:file:b", "u:file:none"])
    assert (got["u:file:a"].gone_pending, got["u:file:a"].gone_why, got["u:file:a"].gone_at,
            got["u:file:a"].mark_ready_at) == (False, "", None, None)
    assert got["u:file:b"].gone_pending and "u:file:none" not in got


async def test_hold_mark(repo):
    await save(repo, doc("a"))
    await repo.mark_gone(U, [notice("a", GONE, T)], drive_identity=D1)
    ripe = T + MIRROR_ARCHIVE_GRACE
    await repo.hold_mark(U, "u:file:a", ripe + timedelta(hours=5))
    assert (await repo.get_docs(U, ["u:file:a"]))["u:file:a"].mark_ready_at == ripe + timedelta(hours=5)
    await repo.hold_mark(U, "u:file:a", ripe)     # never earlier
    assert (await repo.get_docs(U, ["u:file:a"]))["u:file:a"].mark_ready_at == ripe + timedelta(hours=5)
    await repo.hold_mark(U, "u:file:none", ripe)
    assert not await repo.get_docs(U, ["u:file:none"])


async def test_ready_marks_exclude_held_and_unripe(repo):
    await save(repo, doc("late"), doc("early"), doc("held"), doc("fresh"), doc("unmarked"), doc("o2", drive=D2))
    await repo.mark_gone(U, [notice("late", GONE, T - timedelta(hours=3)),
                             notice("early", GONE, T - timedelta(hours=9)),
                             notice("held", GONE, T - timedelta(hours=9)),
                             notice("fresh", GONE, T - timedelta(minutes=5))], drive_identity=D1)
    await repo.mark_gone(U, [notice("o2", GONE, T - timedelta(hours=9), drive=D2)], drive_identity=D2)
    await repo.hold_mark(U, "u:file:held", T + timedelta(hours=1))
    got = await repo.ready_marks(U, D1, T, 10)
    assert keys(got) == ["u:file:early", "u:file:late"]          # earliest ripe first; held, fresh, unmarked, D2 out
    assert keys(await repo.ready_marks(U, D1, T, 1)) == ["u:file:early"]
    assert keys(await repo.ready_marks(U, D2, T, 10)) == ["u:file:o2"]


# -- the step's reads --------------------------------------------------------------------------------------------------

async def test_children_by_parent(repo):
    await save(repo, doc("b", mirror_parent_id="p"), doc("a", mirror_parent_id="p"), doc("c", mirror_parent_id="q"),
               doc("d", mirror_parent_id="p", drive=D2))
    assert keys(await repo.children(U, D1, "p", 10)) == ["u:file:a", "u:file:b"]
    assert keys(await repo.children(U, D1, "p", 1)) == ["u:file:a"]
    assert await repo.children(U, D1, "none", 10) == []


async def test_folder_docs_and_scan_filter_kind(repo):
    await save(repo, doc("f1", KIND_FOLDER), doc("a", KIND_FILE), doc("z", KIND_ARCHIVE), doc("f2", KIND_FOLDER, drive=D2))
    assert keys(await repo.folder_docs(U, D1, None, 10)) == ["u:archive:z", "u:folder:f1"]
    assert keys(await repo.scan_page(U, D1, None, 10)) == ["u:file:a"]


async def test_folder_docs_cursor(repo):
    await save(repo, *[doc(f"f{i}", KIND_FOLDER) for i in range(1, 5)], doc("z", KIND_ARCHIVE))
    first = await repo.folder_docs(U, D1, None, 2)
    assert keys(first) == ["u:archive:z", "u:folder:f1"]
    second = await repo.folder_docs(U, D1, first[-1].key, 2)
    assert keys(second) == ["u:folder:f2", "u:folder:f3"]
    assert keys(await repo.folder_docs(U, D1, "u:folder:f4", 2)) == []


async def test_scan_page_cursor(repo):
    await save(repo, *[doc(f"a{i}") for i in range(1, 6)])
    page = await repo.scan_page(U, D1, None, 2)
    assert keys(page) == ["u:file:a1", "u:file:a2"]
    assert keys(await repo.scan_page(U, D1, page[-1].key, 10)) == ["u:file:a3", "u:file:a4", "u:file:a5"]


async def test_detached_due_before(repo):
    old, recent = T - timedelta(days=10), T - timedelta(days=1)
    await save(repo, doc("old", state=MIRROR_DETACHED, checked_at=old), doc("never", state=MIRROR_DETACHED),
               doc("new", state=MIRROR_DETACHED, checked_at=recent), doc("act", state=MIRROR_ACTIVE, checked_at=old),
               doc("o2", state=MIRROR_DETACHED, drive=D2, checked_at=old))
    before = T - timedelta(days=7)
    assert keys(await repo.detached_due(U, D1, before, 10)) == ["u:file:never", "u:file:old"]
    assert keys(await repo.detached_due(U, D1, before, 1)) == ["u:file:never"]
    assert keys(await repo.detached_due(U, D1, T - timedelta(days=30), 10)) == ["u:file:never"]


async def test_every_step_query_scoped_to_the_drive(repo):
    mine = [doc("a", mirror_parent_id="p", state=MIRROR_DETACHED, checked_at=T - timedelta(days=9))]
    await save(repo, *mine, doc("a2", drive=D2, mirror_parent_id="p", state=MIRROR_DETACHED, checked_at=T - timedelta(days=9)),
               doc("f", KIND_FOLDER, drive=D2), doc("g", drive=D2))
    await repo.mark_gone(U, [notice("g", GONE, T - timedelta(hours=9), drive=D2)], drive_identity=D2)
    assert await repo.ready_marks(U, D1, T, 10) == []
    assert keys(await repo.children(U, D1, "p", 10)) == [mine[0].key]
    assert await repo.folder_docs(U, D1, None, 10) == []
    assert keys(await repo.scan_page(U, D1, None, 10)) == [mine[0].key]
    assert keys(await repo.detached_due(U, D1, T, 10)) == [mine[0].key]


# -- the deleted-files search -------------------------------------------------------------------------------------------

async def test_archived_newest(repo):
    await save(repo, doc("a", state=MIRROR_ARCHIVED, archived_at=T - timedelta(days=2)),
               doc("b", state=MIRROR_ARCHIVED, archived_at=T), doc("c", state=MIRROR_ARCHIVED, archived_at=T - timedelta(days=1), drive=D2),
               doc("d", state=MIRROR_ACTIVE, archived_at=T), doc("e", state=MIRROR_ARCHIVED, archived_at=T, user="o"))
    assert keys(await repo.archived_newest(U, 10)) == ["u:file:b", "u:file:c", "u:file:a"]
    assert keys(await repo.archived_newest(U, 2)) == ["u:file:b", "u:file:c"]


async def test_archived_by_keys_filters_state_across_drives(repo):
    await save(repo, doc("a", state=MIRROR_ARCHIVED, archive_keys=("w:x", "w:y"), name_keys=("w:na",)),
               doc("b", state=MIRROR_ARCHIVED, drive=D2, archive_keys=("w:y",), name_keys=("w:nb",)),
               doc("c", state=MIRROR_DETACHED, archive_keys=("w:x",)), doc("d", state=MIRROR_ARCHIVED, user="o", archive_keys=("w:x",)),
               doc("e", state=MIRROR_ARCHIVED, archive_keys=("w:z",)))
    assert keys(await repo.archived_by_keys(U, ARCHIVE_FAMILY, ["w:x", "w:y"], 10)) == ["u:file:a", "u:file:b"]
    assert keys(await repo.archived_by_keys(U, ARCHIVE_FAMILY, ["w:y"], 1)) == ["u:file:a"]
    assert keys(await repo.archived_by_keys(U, NAME_FAMILY, ["w:nb", "w:na"], 10)) == ["u:file:a", "u:file:b"]
    assert await repo.archived_by_keys(U, ARCHIVE_FAMILY, [], 10) == []
    with pytest.raises(ValueError):
        await repo.archived_by_keys(U, "other", ["w:x"], 10)


# -- pins ------------------------------------------------------------------------------------------------------------------

async def test_pinned_archived_detached_and_gone_pending_not_active(repo):
    await save(repo, doc("a", state=MIRROR_ARCHIVED, content_key="k-arch"), doc("b", state=MIRROR_DETACHED, content_key="k-det"),
               doc("c", state=MIRROR_ACTIVE, content_key="k-pend"), doc("d", state=MIRROR_ACTIVE, content_key="k-act"),
               doc("e", state=MIRROR_ACTIVE, content_key=""), doc("f", user="o", state=MIRROR_ARCHIVED, content_key="k-other"))
    await repo.mark_gone(U, [notice("c", GONE, T)], drive_identity=D1)
    assert [await repo.pinned(U, k) for k in ("k-arch", "k-det", "k-pend", "k-act", "k-other", "", "none")] == [
        True, True, True, False, False, False, False]
    assert await repo.pinned_keys(U) == frozenset({"k-arch", "k-det", "k-pend"})
    assert await repo.pinned_keys("nobody") == frozenset()


async def test_pins_span_every_drive(repo):
    await save(repo, doc("a", drive=D1, state=MIRROR_ARCHIVED, content_key="k1"),
               doc("b", drive=D2, state=MIRROR_DETACHED, content_key="k2"))
    assert await repo.pinned(U, "k1") and await repo.pinned(U, "k2")
    assert await repo.pinned_keys(U) == frozenset({"k1", "k2"})


async def test_pinned_keys_paged(adapter, monkeypatch):
    repo, db = adapter
    if db is not None:
        monkeypatch.setattr("src.adapters.firestore_drive_mirror_repo.PIN_PAGE", 3)
    many = [doc(f"i{n:02d}", state=MIRROR_ARCHIVED, content_key=f"k{n % 7:02d}") for n in range(12)]   # duplicates too
    pending = [doc(f"p{n}", content_key=f"pk{n}") for n in range(4)]
    await save(repo, *many, *pending)
    await repo.mark_gone(U, [notice(f"p{n}", GONE, T) for n in range(4)], drive_identity=D1)
    want = frozenset({f"k{n:02d}" for n in range(7)} | {f"pk{n}" for n in range(4)})
    assert await repo.pinned_keys(U) == want


# -- the Firestore adapter alone --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("error", [ServiceUnavailable("down"), FailedPrecondition("The query requires an index.")])
async def test_backend_error_is_store_unavailable(adapter, error):
    repo, db = adapter
    if db is None:
        pytest.skip("Firestore only")
    for name in (LEDGER, ROOTS):
        db.collection(name).fail = error
    calls = [repo.roots(U), repo.ready_marks(U, D1, T, 5), repo.children(U, D1, "p", 5), repo.folder_docs(U, D1, None, 5),
             repo.scan_page(U, D1, None, 5), repo.detached_due(U, D1, T, 5), repo.archived_newest(U, 5),
             repo.archived_by_keys(U, NAME_FAMILY, ["w:a"], 5), repo.pinned(U, "k"), repo.pinned_keys(U),
             repo.by_mirror_item(U, "m")]
    for call in calls:
        with pytest.raises(DriveIndexStoreUnavailable):
            await call
