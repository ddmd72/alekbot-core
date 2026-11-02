"""A root move on a full drive (slice 2b, MULTI_DRIVE_RFC §11.3): the old root becomes a previous root at once, but no
new root is created while the quota hold lasts, and nothing else of the step runs."""
from dataclasses import replace
from datetime import timedelta

from src.domain.drive_index import DriveIndexRecord, Reason, RecordState
from src.domain.drive_mirror import ROOT_NAME

from .drive_mirror_fakes import AREA, DRIVE_IDENTITY, USER, MirrorWorld


async def test_a_root_move_under_a_quota_hold_creates_no_new_root_yet():
    w = MirrorWorld()
    await w.run(write_folder_id="")
    old = await w.mirror.get_root(USER, DRIVE_IDENTITY)
    await w.put(DriveIndexRecord(USER, old.root_item_id, parent_id=AREA, name=ROOT_NAME, is_folder=True,
                                 state=RecordState.METADATA_ONLY, reason=Reason.FOLDER))
    await w.mirror.save_root(replace(old, quota_hold_until=w.clock + timedelta(hours=6)))
    inbox = await w.folder("Inbox")
    created_before = len([n for n in w.drive.nodes.values() if n.name == ROOT_NAME])

    counters = await w.run(write_folder_id=inbox)

    held = await w.mirror.get_root(USER, DRIVE_IDENTITY)
    assert counters.get("mirror_root_moved") == 1
    assert held.root_item_id == old.root_item_id and old.root_item_id in held.previous_root_ids
    assert len([n for n in w.drive.nodes.values() if n.name == ROOT_NAME]) == created_before
