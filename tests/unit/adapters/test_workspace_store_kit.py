"""P11 card A: the real GcsWorkspaceAdapter behind WorkspaceStorePort, over the shared GCS fake (tests/support/gcs_fake.py,
a versioned bucket). The contract test (test_workspace_store_contract.py) already covers create, overwrite, stale
writes, delete, exact names, isolation, the meta round-trip and the read-backs; this kit adds the axes it lacks:
tenancy at the prefix boundary, listing contents, legacy objects, write fields, reads of old and vanished versions,
idempotency of delete, and the stamp a failed delete leaves (P11-G4-1). The `put` generation precondition is held by
the contract test (create on an existing path, stale overwrite), not by this kit."""
import pytest

from src.adapters.gcs_workspace_adapter import GcsWorkspaceAdapter
from src.domain.exceptions import WorkspaceConflict
from src.domain.workspace import WorkspaceDeleteMeta, WorkspaceWriteMeta, render_history
from tests.support.gcs_fake import FakeGcs
from tests.support.redelivery_world import require

BUCKET = "workspace-test"
CHAT = WorkspaceWriteMeta(origin="chat")
FIRE = WorkspaceWriteMeta(origin="background:execute_reminder", fire="reminder:n1@2026-10-20T06:00:00+00:00")
P = "projects/a.md"


def _adapter(fake: FakeGcs) -> GcsWorkspaceAdapter:
    return GcsWorkspaceAdapter(BUCKET, client=fake.client())


@pytest.fixture
def fake():
    return FakeGcs(buckets={BUCKET: "versioned"})


@pytest.fixture
def store(fake):
    return _adapter(fake)


def _raw(fake, name, data, metadata=None):
    """An object written by something other than the adapter (legacy: no or foreign metadata)."""
    blob = fake.client().bucket(BUCKET).blob(name)
    if metadata is not None:
        blob.metadata = metadata
    blob.upload_from_string(data, content_type="text/markdown")
    return blob


async def test_tenancy_prefix_boundary(fake, store):
    other = await store.put("u10", P, b"Rome hotel RM-55120", CHAT, 0)
    assert await store.list("u1", "") == []
    assert await store.list("u1", "projects/") == []
    assert await store.list("u1", P, include_versions=True) == []
    with pytest.raises(FileNotFoundError):
        await store.read("u1", P)
    mine = await store.put("u1", P, b"Lisbon trip notes", CHAT, 0)
    assert mine.path == P
    assert [v.generation for v in fake.versions(BUCKET, "u10/" + P)] == [other.generation]
    assert fake.live(BUCKET, "u10/" + P).data == b"Rome hotel RM-55120"
    assert [e.path for e in await store.list("u10", "")] == [P]


async def test_exact_file_history_excludes_longer_names(store):
    a1 = await store.put("u1", P, b"v1", CHAT, 0)
    a2 = await store.put("u1", P, b"v2", CHAT, a1.generation)
    await store.put("u1", P + ".bak", b"backup", CHAT, 0)
    history = await store.list("u1", P, include_versions=True)
    assert sorted(e.generation for e in history) == [a1.generation, a2.generation]
    assert {e.path for e in history} == {P}


async def test_folder_listing_live_only_and_with_versions(store):
    a1 = await store.put("u1", "projects/a.md", b"a1", CHAT, 0)
    a2 = await store.put("u1", "projects/a.md", b"a2", CHAT, a1.generation)         # overwritten once
    b = await store.put("u1", "projects/b.md", b"b", CHAT, 0)
    c = await store.put("u1", "projects/c.md", b"c", CHAT, 0)
    await store.delete("u1", "projects/c.md", c.generation, WorkspaceDeleteMeta(deleted_by="chat"))   # deleted
    await store.put("u1", "references/rome-trip.md", b"other folder", CHAT, 0)
    live = await store.list("u1", "projects/")
    assert sorted((e.path, e.generation) for e in live) == [("projects/a.md", a2.generation), ("projects/b.md", b.generation)]
    everything = {e.generation: e for e in await store.list("u1", "projects/", include_versions=True)}
    assert set(everything) == {a1.generation, a2.generation, b.generation, c.generation}
    assert {g for g, e in everything.items() if e.live} == {a2.generation, b.generation}
    assert {g for g, e in everything.items() if e.deleted_at is not None} == {a1.generation, c.generation}
    assert everything[c.generation].deleted_by == "chat" and everything[a1.generation].deleted_by is None


async def test_legacy_object_without_metadata(fake, store):
    _raw(fake, "u1/" + P, b"written before stamps")
    [entry] = await store.list("u1", P)
    assert (entry.origin, entry.write_id, entry.fire, entry.restored_from) == ("", None, None, None)
    blob = await store.read("u1", P)
    assert blob.data == b"written before stamps" and blob.entry.generation == entry.generation


async def test_legacy_restored_from_not_numeric(fake, store):
    _raw(fake, "u1/" + P, b"x", {"origin": "chat", "restored_from": "abc"})
    [entry] = await store.list("u1", P)
    assert (entry.origin, entry.restored_from) == ("chat", None)


async def test_scratch_put_sets_custom_time_other_folders_do_not(fake, store):
    await store.put("u1", "scratch/d.md", b"draft", CHAT, 0)
    await store.put("u1", P, b"kept", CHAT, 0)
    scratch, project = fake.live(BUCKET, "u1/scratch/d.md"), fake.live(BUCKET, "u1/" + P)
    assert scratch.custom_time is not None and scratch.custom_time.tzinfo is not None
    assert project.custom_time is None
    assert (scratch.content_type, project.metadata["origin"]) == ("text/markdown; charset=utf-8", "chat")


async def test_put_stamps_fire_and_restored_from(fake, store):
    e1 = await store.put("u1", P, b"v1", FIRE, 0)
    e2 = await store.put("u1", P, b"v1 again", WorkspaceWriteMeta(origin="chat", restored_from=e1.generation),
                         e1.generation)
    stored = fake.live(BUCKET, "u1/" + P).metadata
    assert stored["restored_from"] == str(e1.generation) and stored["write_id"] == e2.write_id
    assert "fire" not in stored
    assert fake.versions(BUCKET, "u1/" + P)[0].metadata["fire"] == FIRE.fire


async def test_read_of_a_noncurrent_generation(store):
    e1 = await store.put("u1", P, b"v1", CHAT, 0)
    await store.put("u1", P, b"v2", CHAT, e1.generation)
    blob = await store.read("u1", P, generation=e1.generation)
    assert blob.data == b"v1"
    assert (blob.entry.generation, blob.entry.live) == (e1.generation, False) and blob.entry.deleted_at is not None


async def test_read_of_an_erased_generation_is_missing(fake, store):
    e1 = await store.put("u1", P, b"v1", CHAT, 0)
    await store.put("u1", P, b"v2", CHAT, e1.generation)
    fake.client().bucket(BUCKET).blob("u1/" + P, generation=e1.generation).delete()      # erased for good
    with pytest.raises(FileNotFoundError):
        await store.read("u1", P, generation=e1.generation)
    assert (await store.read("u1", P)).data == b"v2"


async def test_version_gone_between_metadata_and_media(fake, store):
    e1 = await store.put("u1", P, b"v1", CHAT, 0)
    erased = []

    def erase():
        fake.client().bucket(BUCKET).blob("u1/" + P, generation=e1.generation).delete()
        erased.append(True)

    fake.on("get", erase)          # after the metadata GET, before the media GET
    with pytest.raises(FileNotFoundError):
        await store.read("u1", P)
    assert erased == [True]


async def test_second_delete_is_missing(fake, store):
    e1 = await store.put("u1", P, b"v1", CHAT, 0)
    await store.delete("u1", P, e1.generation, WorkspaceDeleteMeta(deleted_by="chat"))
    with pytest.raises(FileNotFoundError):
        await store.delete("u1", P, e1.generation, WorkspaceDeleteMeta(deleted_by="chat"))
    [v] = fake.versions(BUCKET, "u1/" + P)
    assert not v.live and v.metadata["deleted_by"] == "chat"


async def test_stale_delete_stamps_nothing(fake, store):
    """Control for P11-G4-1: a delete whose generation is already stale is refused at the patch, before any write."""
    e1 = await store.put("u1", P, b"v1", CHAT, 0)
    await store.put("u1", P, b"v2", FIRE, e1.generation)
    with pytest.raises(WorkspaceConflict):
        await store.delete("u1", P, e1.generation, WorkspaceDeleteMeta(deleted_by="chat"))
    assert all("deleted_by" not in v.metadata for v in fake.versions(BUCKET, "u1/" + P))
    history = render_history(P, await store.list("u1", P, include_versions=True))
    assert "deleted by" not in history and "replaced" in history


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="P11-G4-1: a delete that loses to an overwrite leaves its stamp on the replaced version")
async def test_delete_that_loses_to_an_overwrite_leaves_no_stamp(fake, store):
    """A chat delete and a reminder fire hold the same generation g1. The delete's metadata patch lands on g1; the
    fire's write lands before the delete request, which then meets 412. g1 was replaced, not deleted, yet history
    says "deleted by chat". Flips when `_delete_sync` clears the stamp on generation g1 when its delete did not land
    (or when `history` reads such a version as replaced)."""
    e1 = await store.put("u1", P, b"v1", CHAT, 0)
    overwrote = []

    def fire_overwrites():
        blob = fake.client().bucket(BUCKET).blob("u1/" + P)
        blob.metadata = {"origin": "background:execute_reminder", "write_id": "w-fire"}
        blob.upload_from_string(b"v2", content_type="text/markdown; charset=utf-8", if_generation_match=e1.generation)
        overwrote.append(blob.generation)

    fake.on("delete", fire_overwrites, when="before")
    err = None
    try:
        await store.delete("u1", P, e1.generation, WorkspaceDeleteMeta(deleted_by="chat"))
    except Exception as exc:  # captured: the outcome is the history, not the raise
        err = exc
    require(overwrote != [], "the fire's write landed between the delete's two requests")
    require(isinstance(err, WorkspaceConflict), f"the delete lost to the write (got {err!r})")
    require(fake.live(BUCKET, "u1/" + P).generation == overwrote[0] != e1.generation, "the fire's version is live")
    history = render_history(P, await store.list("u1", P, include_versions=True))
    g1_line = next((line for line in history.splitlines() if line.strip().startswith(f"{e1.generation} ")), None)
    require(g1_line is not None, "history lists g1")
    assert "deleted by" not in g1_line
