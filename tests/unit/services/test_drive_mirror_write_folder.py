"""The transcript mirror under the write folder (MULTI_DRIVE_RFC §11.3, D7, review R1 N6): no write folder, no mirror;
the root is created under the write folder; the drive's root as write folder keeps the area root; a stored root that is
not under the current write folder is a root move (the old root becomes a previous root, a new one is created)."""
from src.domain.drive_index import DriveIndexRecord, Reason, RecordState
from src.domain.drive_mirror import ROOT_NAME

from .drive_mirror_fakes import AREA, DRIVE_IDENTITY, USER, MirrorWorld

ROOT_FOLDER = ""


async def current_root(w: MirrorWorld):
    return await w.mirror.get_root(USER, DRIVE_IDENTITY)


def writes(w: MirrorWorld):
    return list(w.drive.writes())


async def test_without_a_write_folder_there_is_no_mirror_and_nothing_is_written():
    w = MirrorWorld()
    await w.recording("a.mp4")
    counters = await w.run(write_folder_id=None)
    assert counters.get("mirror_no_write_folder") == 1
    assert await current_root(w) is None
    assert writes(w) == []


async def test_the_root_is_created_under_a_subfolder_write_folder():
    w = MirrorWorld()
    inbox = await w.folder("Inbox")
    await w.run(write_folder_id=inbox)
    root = await current_root(w)
    assert root is not None
    assert w.drive.nodes[root.root_item_id].parent_id == inbox


async def test_the_drives_root_as_write_folder_keeps_the_area_root():
    w = MirrorWorld()
    await w.run(write_folder_id=ROOT_FOLDER)
    root = await current_root(w)
    assert root is not None and w.drive.nodes[root.root_item_id].parent_id == AREA


async def test_a_root_outside_the_new_write_folder_is_a_root_move():
    w = MirrorWorld()
    await w.run(write_folder_id=ROOT_FOLDER)
    old = (await current_root(w)).root_item_id
    await w.put(DriveIndexRecord(USER, old, parent_id=AREA, name=ROOT_NAME, is_folder=True,
                                 state=RecordState.METADATA_ONLY, reason=Reason.FOLDER))
    inbox = await w.folder("Inbox")
    counters = await w.run(write_folder_id=inbox)
    new = await current_root(w)
    assert counters.get("mirror_root_moved") == 1
    assert new.root_item_id != old and old in new.previous_root_ids
    assert w.drive.nodes[new.root_item_id].parent_id == inbox


async def test_a_root_already_under_the_write_folder_is_left_alone():
    w = MirrorWorld()
    inbox = await w.folder("Inbox")
    await w.run(write_folder_id=inbox)
    root = await current_root(w)
    await w.put(DriveIndexRecord(USER, root.root_item_id, parent_id=inbox, name=ROOT_NAME, is_folder=True,
                                 state=RecordState.METADATA_ONLY, reason=Reason.FOLDER))
    counters = await w.run(write_folder_id=inbox)
    assert not counters.get("mirror_root_moved")
    assert (await current_root(w)).root_item_id == root.root_item_id


async def test_the_orphan_root_audit_covers_the_write_folder_where_the_root_lives():
    """The audit lists the folder roots are created in: an orphan named like the root, under a subfolder write folder, is
    found (reported once, never adopted)."""
    from datetime import timedelta

    from src.domain.drive_index_params import MIRROR_AUDIT_DAYS
    w = MirrorWorld()
    await w.recording("a.mp4")
    inbox = await w.folder("Inbox")
    await w.run(write_folder_id=inbox)
    orphan = w.drive.add_folder(inbox, "_Transcripts (Alek)")
    w.advance(timedelta(days=MIRROR_AUDIT_DAYS, hours=1))
    counters = await w.run(write_folder_id=inbox)
    root = await current_root(w)
    assert counters["mirror_orphan_roots"] == 1 and root.orphans_reported
    assert root.root_item_id != orphan
