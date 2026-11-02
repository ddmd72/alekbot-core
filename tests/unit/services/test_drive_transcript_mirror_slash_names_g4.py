"""G4 (GOOGLE_DRIVE_RFC): the mirror prints a '/' inside a folder name as NAME_SLASH, in the root path and in a
transcript's source path. Synthetic names only."""
from __future__ import annotations

from collections import Counter

from src.domain.drive_index import DriveIndexRecord, Reason, RecordState
from src.domain.user_drive import NAME_SLASH
from src.services.drive_transcript_mirror import _Run

from .drive_mirror_fakes import AREA, HANDLE, USER, MirrorWorld

GARDEN = "Garden plan 2026/27"
GARDEN_PRINTED = f"Garden plan 2026{NAME_SLASH}27"


async def test_a_transcripts_source_path_escapes_a_slash_in_a_folder_name():
    w = MirrorWorld()
    garden = await w.folder(GARDEN)
    a = await w.recording("Meeting.mp4", garden)
    await w.run()
    assert w.doc(a).source_path == GARDEN_PRINTED


async def test_a_plain_chain_keeps_its_separators():
    w = MirrorWorld()
    garden = await w.folder(GARDEN)
    sub = await w.folder("Beds", garden)
    a = await w.recording("Meeting.mp4", sub)
    await w.run()
    assert w.doc(a).source_path == f"{GARDEN_PRINTED}/Beds"


async def test_the_mirror_root_path_escapes_a_slash_in_the_roots_name():
    w = MirrorWorld()
    await w.recording("a.mp4")
    await w.run()
    root_id = w.root.root_item_id
    await w.put(DriveIndexRecord(USER, root_id, parent_id=AREA, name=GARDEN, is_folder=True,
                                 state=RecordState.METADATA_ONLY, reason=Reason.FOLDER))
    ctx = _Run(USER, HANDLE, w.host(), Counter(), root=w.root, area_root_id=AREA)
    assert await w.service._root_path(ctx) == GARDEN_PRINTED
