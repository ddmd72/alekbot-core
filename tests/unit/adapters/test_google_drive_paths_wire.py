"""Wire — paths (N1) and outer folders (GOOGLE_DRIVE_RFC §3.2 N1 and row `outer_folders`, G4, G8). A path walks
`parents` up to My Drive's root through a per-connection cache (5 minutes), joined with `join_drive_path` (a `/` in a
name prints as `∕`, the name is unchanged); a folder change seen in the feed drops its entry; a chain stops at an item
with no visible parent, at a member shared drive's root (named from `drives.get`) or at MAX_CHAIN_DEPTH. `outer_folders`
is the parentless folders plus the member drive roots, cached 5 minutes per connection and dropped by `forget`."""
import pytest

from src.adapters.google import google_drive_adapter
from src.adapters.google.google_drive_adapter import CACHE_TTL_S
from src.domain.drive_search import MAX_CHAIN_DEPTH
from tests.contracts.user_drive_contract import drain
from tests.support.google_drive_fake import google_setup


@pytest.fixture
async def setup(monkeypatch):
    return await google_setup(monkeypatch)


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(google_drive_adapter, "_clock", lambda: now[0])
    return now


def _walks(setup):
    return [r for r in setup.fake.calls("files.get") if r.params.get("fields") == "id,name,parents,driveId,trashed"]


async def test_a_name_with_a_slash_prints_escaped_and_keeps_its_name(setup):
    projects = setup.a.add_folder(setup.a.root.id, "Projects")
    plan = setup.a.add_folder(projects.id, "Garden plan 2026/27")
    notes = setup.a.add_file(plan.id, "notes.md", b"n")
    item = await setup.adapter.get_item(setup.handle, notes.id)
    folder = await setup.adapter.get_item(setup.handle, plan.id)
    assert item.path == "Projects/Garden plan 2026∕27/notes.md"
    assert folder.name == "Garden plan 2026/27" and folder.path == "Projects/Garden plan 2026∕27"
    listed = await setup.adapter.list_children(setup.handle, plan.id)
    assert [c.path for c in listed] == ["Projects/Garden plan 2026∕27/notes.md"]


async def test_the_walk_is_cached_per_connection(setup, clock):
    projects = setup.a.add_folder(setup.a.root.id, "Projects")
    a = setup.a.add_file(projects.id, "a.txt", b"a")
    b = setup.a.add_file(projects.id, "b.txt", b"b")
    await setup.adapter.get_item(setup.handle, a.id)
    walks = len(_walks(setup))
    assert walks == 1
    await setup.adapter.get_item(setup.handle, b.id)
    assert len(_walks(setup)) == walks                       # served from the cache
    clock[0] += CACHE_TTL_S + 1
    await setup.adapter.get_item(setup.handle, b.id)
    assert len(_walks(setup)) == walks + 1                   # expired after 5 minutes
    assert setup.handle.connection_id in setup.adapter._paths
    assert setup.other.connection_id not in setup.adapter._paths
    await setup.adapter.forget(setup.handle)
    assert setup.handle.connection_id not in setup.adapter._paths


async def test_a_folder_rename_seen_in_the_feed_drops_its_entry(setup, clock):
    projects = setup.a.add_folder(setup.a.root.id, "Projects")
    talk = setup.a.add_file(projects.id, "Hamburg talk.mp4", b"talk")
    _, cursor = await drain(setup.adapter, setup.handle)
    assert (await setup.adapter.get_item(setup.handle, talk.id)).path == "Projects/Hamburg talk.mp4"
    projects.name = "Talks"
    setup.a.touch(projects)
    await setup.adapter.get_changes(setup.handle, cursor)
    assert (await setup.adapter.get_item(setup.handle, talk.id)).path == "Talks/Hamburg talk.mp4"


async def test_a_folder_trashed_in_the_feed_drops_its_entry(setup, clock):
    projects = setup.a.add_folder(setup.a.root.id, "Projects")
    talk = setup.a.add_file(projects.id, "Hamburg talk.mp4", b"talk")
    _, cursor = await drain(setup.adapter, setup.handle)
    await setup.adapter.get_item(setup.handle, talk.id)
    setup.a.trash(projects)
    await setup.adapter.get_changes(setup.handle, cursor)
    assert projects.id not in setup.adapter._paths.get(setup.handle.connection_id, {})


async def test_a_chain_without_a_visible_parent_prints_from_its_top(setup):
    top = setup.a.add_folder(None, "Acme Hamburg")             # shared to the account, its parent not visible
    q3 = setup.a.add_folder(top.id, "Q3")
    budget = setup.a.add_file(q3.id, "Q3 budget", b"b")
    assert (await setup.adapter.get_item(setup.handle, budget.id)).path == "Acme Hamburg/Q3/Q3 budget"


async def test_a_parent_that_cannot_be_read_ends_the_chain(setup):
    hidden = setup.a.add_folder(setup.a.root.id, "Hidden")
    q3 = setup.a.add_folder(hidden.id, "Q3")
    budget = setup.a.add_file(q3.id, "Q3 budget", b"b")
    setup.a.closed.add(hidden.id)
    assert (await setup.adapter.get_item(setup.handle, budget.id)).path == "Q3/Q3 budget"


async def test_a_member_drive_root_is_named_from_drives_get(setup):
    drive_id = setup.a.add_shared_drive("Acme")
    q3 = setup.a.add_folder(drive_id, "Q3", drive_id=drive_id)
    budget = setup.a.add_file(q3.id, "Q3 budget", b"b", drive_id=drive_id)
    item = await setup.adapter.get_item(setup.handle, budget.id)
    assert item.path == "Acme/Q3/Q3 budget"
    assert [r.url.rsplit("/", 1)[1] for r in setup.fake.calls("drives.get")] == [drive_id]


async def test_a_chain_is_cut_at_the_maximum_depth(setup):
    parent = setup.a.root.id
    depth = MAX_CHAIN_DEPTH + 6
    for n in range(depth):
        parent = setup.a.add_folder(parent, f"d{n}").id
    leaf = setup.a.add_file(parent, "leaf.txt", b"x")
    path = (await setup.adapter.get_item(setup.handle, leaf.id)).path
    segments = path.split("/")
    # the leaf and the MAX_CHAIN_DEPTH - 1 folders above it: d{depth-1} down to d{depth - MAX_CHAIN_DEPTH + 1}
    assert len(segments) == MAX_CHAIN_DEPTH and segments[-1] == "leaf.txt"
    assert segments[0] == f"d{depth - MAX_CHAIN_DEPTH + 1}"
    assert len(_walks(setup)) == MAX_CHAIN_DEPTH - 1


async def test_outer_folders_are_the_parentless_folders_and_the_member_drive_roots(setup, clock):
    shared = setup.a.add_folder(None, "Acme Hamburg")
    setup.a.add_folder(shared.id, "Q3")
    setup.a.add_folder(setup.a.root.id, "Projects")
    setup.a.add_file(None, "loose.pdf", b"p")                 # a parentless file is not a folder
    drive_id = setup.a.add_shared_drive("Acme")
    tops = await setup.adapter.outer_folders(setup.handle)
    assert [(t.item_id, t.name, t.path, t.is_folder) for t in tops] == [
        (drive_id, "Acme", "Acme", True), (shared.id, "Acme Hamburg", "Acme Hamburg", True)]
    assert tops[0].identity == f"google_drive:{drive_id}"
    listing = setup.fake.calls("files.list")[0].params
    assert listing["q"] == "mimeType='application/vnd.google-apps.folder' and trashed=false"
    assert await setup.adapter.outer_folders(setup.other) == []


async def test_outer_folders_are_cached_five_minutes_and_dropped_by_forget(setup, clock):
    setup.a.add_folder(None, "Acme Hamburg")
    await setup.adapter.outer_folders(setup.handle)
    lists = len(setup.fake.calls("files.list"))
    await setup.adapter.outer_folders(setup.handle)
    assert len(setup.fake.calls("files.list")) == lists
    clock[0] += CACHE_TTL_S + 1
    await setup.adapter.outer_folders(setup.handle)
    assert len(setup.fake.calls("files.list")) == lists + 1
    await setup.adapter.forget(setup.handle)
    await setup.adapter.outer_folders(setup.handle)
    assert len(setup.fake.calls("files.list")) == lists + 2


async def test_an_outer_folder_name_with_a_slash_prints_escaped(setup):
    setup.a.add_folder(None, "Garden plan 2026/27")
    tops = await setup.adapter.outer_folders(setup.handle)
    assert [(t.name, t.path) for t in tops] == [("Garden plan 2026/27", "Garden plan 2026∕27")]
