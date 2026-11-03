"""Wire — the feed defers a native's head revision (GOOGLE_DRIVE_RFC GE-36, G6, GE-4). An enumeration page and a feed
page read no revision list: an exportable native that allows reading revisions comes back pending with its
`m:<modifiedTime>` key; binaries and natives without `canReadRevisions` carry their final key. `resolve_content_versions`
gives a pending change `r:<last revision across pages>`, keeps `m:` when the list is refused, runs at most 4 lists at
once, leaves other changes untouched and calls nothing when none is pending. `get_change`, `list_children_changes` and
`child_by_name` still read the revision at once. Served by the stateful Drive fake."""
import pytest

from src.adapters.google import google_drive_adapter
from src.adapters.google.google_drive_adapter import REVISION_CALLS_IN_FLIGHT
from src.domain.drive_index import DriveChange
from tests.contracts.user_drive_contract import drain
from tests.support.google_drive_fake import DOC, SHEET, XLSX, google_setup

_MD = {"text/markdown": b"plan"}


@pytest.fixture
async def setup(monkeypatch):
    return await google_setup(monkeypatch)


def _by_id(changes):
    return {c.item_id: c for c in changes}


async def test_an_enumeration_page_reads_no_revisions_and_returns_natives_pending(setup):
    folder = setup.a.add_folder(setup.a.root.id, "R&D")
    plan = setup.a.add_native(folder.id, "Tom's plan", DOC, exports=_MD, revisions=["rev-1", "rev-3"])
    page = await setup.adapter.get_changes(setup.handle, None)
    change = _by_id(page.changes)[plan.id]
    assert setup.fake.calls("revisions.list") == []
    assert change.content_version_pending is True and change.content_version == f"m:{plan.modified}"
    assert change.modified_at is not None and change.name == "Tom's plan"
    assert _by_id(page.changes)[folder.id].content_version_pending is False


async def test_a_feed_page_reads_no_revisions_and_returns_natives_pending(setup):
    _, cursor = await drain(setup.adapter, setup.handle)
    folder = setup.a.add_folder(setup.a.root.id, "R&D")
    plan = setup.a.add_native(folder.id, "Tom's plan", DOC, exports=_MD)
    page = await setup.adapter.get_changes(setup.handle, cursor)
    assert not page.full_enumeration
    change = _by_id(page.changes)[plan.id]
    assert change.content_version_pending is True and change.content_version == f"m:{plan.modified}"
    assert setup.fake.calls("revisions.list") == []


async def test_binaries_and_natives_without_read_revisions_are_never_pending(setup):
    binary = setup.a.add_file(setup.a.root.id, "f-1.pdf", b"x")
    closed = setup.a.add_native(setup.a.root.id, "f-2", SHEET, exports={XLSX: b"x"}, can_read_revisions=False)
    trashed = setup.a.add_native(setup.a.root.id, "f-3", DOC, exports=_MD)
    _, cursor = await drain(setup.adapter, setup.handle)
    setup.a.trash(trashed)
    page = await setup.adapter.get_changes(setup.handle, cursor)
    enumerated = _by_id((await setup.adapter.get_changes(setup.handle, None)).changes)
    assert enumerated[binary.id].content_version == binary.head and not enumerated[binary.id].content_version_pending
    assert enumerated[closed.id].content_version == f"m:{closed.modified}"
    assert enumerated[closed.id].content_version_pending is False
    gone = _by_id(page.changes)[trashed.id]
    assert gone.deleted and gone.content_version_pending is False
    assert setup.fake.calls("revisions.list") == []


async def test_resolve_gives_the_last_revision_across_pages(setup, monkeypatch):
    monkeypatch.setattr(google_drive_adapter, "PAGE_SIZE", 2)
    plan = setup.a.add_native(setup.a.root.id, "Tom's plan", DOC, exports=_MD, revisions=["rev-1", "rev-2", "rev-3"])
    changes, _ = await drain(setup.adapter, setup.handle)
    pending = [c for c in changes if c.content_version_pending]
    [resolved] = await setup.adapter.resolve_content_versions(setup.handle, pending)
    assert resolved.item_id == plan.id and resolved.content_version == "r:rev-3"
    assert resolved.content_version_pending is False
    calls = setup.fake.calls("revisions.list")
    assert len(calls) == 2 and calls[1].params["pageToken"]
    assert calls[0].params["fields"] == "nextPageToken,revisions(id)"
    assert resolved.content_version == (await setup.adapter.get_change(setup.handle, plan.id)).content_version


@pytest.mark.parametrize("status,reason", [(403, "insufficientFilePermissions"), (404, "notFound"), (403, "")])
async def test_a_refused_list_keeps_the_modified_time_key(setup, status, reason):
    plan = setup.a.add_native(setup.a.root.id, "Tom's plan", DOC, exports=_MD)
    changes, _ = await drain(setup.adapter, setup.handle)
    setup.fake.fail("revisions.list", status, reason)
    [resolved] = await setup.adapter.resolve_content_versions(
        setup.handle, [c for c in changes if c.item_id == plan.id])
    assert resolved.content_version == f"m:{plan.modified}" and resolved.content_version_pending is False


async def test_at_most_four_lists_run_at_once_and_order_is_kept(setup):
    folder = setup.a.add_folder(setup.a.root.id, "folder-a")
    plans = [setup.a.add_native(folder.id, f"f-{n}", DOC, exports=_MD, revisions=[f"rev-{n}"]) for n in range(10)]
    changes, _ = await drain(setup.adapter, setup.handle)
    resolved = await setup.adapter.resolve_content_versions(setup.handle, changes)
    assert [c.item_id for c in resolved] == [c.item_id for c in changes]
    by_id = _by_id(resolved)
    assert all(by_id[p.id].content_version == f"r:rev-{n}" for n, p in enumerate(plans))
    assert len(setup.fake.calls("revisions.list")) == 10
    assert setup.fake.max_in_flight["revisions.list"] == REVISION_CALLS_IN_FLIGHT == 4


async def test_changes_that_are_not_pending_are_returned_untouched_without_a_call(setup):
    binary = setup.a.add_file(setup.a.root.id, "f-1.pdf", b"x")
    folder = setup.a.add_folder(setup.a.root.id, "folder-a")
    changes, _ = await drain(setup.adapter, setup.handle)
    deleted = DriveChange(item_id="f-9", deleted=True)
    given = [*changes, deleted]
    before = len(setup.fake.requests)
    assert await setup.adapter.resolve_content_versions(setup.handle, given) == given
    assert len(setup.fake.requests) == before
    assert {binary.id, folder.id} <= {c.item_id for c in changes}


async def test_resolve_mixes_pending_and_final_changes_in_order(setup):
    binary = setup.a.add_file(setup.a.root.id, "f-1.pdf", b"x")
    plan = setup.a.add_native(setup.a.root.id, "Tom's plan", DOC, exports=_MD, revisions=["rev-3"])
    changes, _ = await drain(setup.adapter, setup.handle)
    resolved = await setup.adapter.resolve_content_versions(setup.handle, changes)
    assert [c.item_id for c in resolved] == [c.item_id for c in changes]
    by_id = _by_id(resolved)
    assert by_id[binary.id] == _by_id(changes)[binary.id]
    assert by_id[plan.id].content_version == "r:rev-3"
    assert [r.params for r in setup.fake.calls("revisions.list")][0]["pageSize"] == "1000"
    assert len(setup.fake.calls("revisions.list")) == 1


async def test_id_reads_and_listings_still_resolve_at_once(setup):
    folder = setup.a.add_folder(setup.a.root.id, "R&D")
    plan = setup.a.add_native(folder.id, "Tom's plan", DOC, exports=_MD, revisions=["rev-3"])
    one = await setup.adapter.get_change(setup.handle, plan.id)
    listed = (await setup.adapter.list_children_changes(setup.handle, folder.id, 10)).changes[0]
    named = await setup.adapter.child_by_name(setup.handle, folder.id, "Tom's plan")
    item = await setup.adapter.get_item(setup.handle, plan.id)
    for change in (one, listed, named):
        assert change is not None and change.content_version == "r:rev-3" and change.content_version_pending is False
    assert item.content_version == "r:rev-3"
    assert len(setup.fake.calls("revisions.list")) == 4
