"""A source chain that ends at a folder with no visible parent is placed, not refused (2026-11-05).

Live: on a folder-scoped Google connection the selected folder's top scope ancestor came from the provider with no
parent (a folder shared with the owner), so `_source_chain` raised `_Unplaceable` for every recording under it and
the transcripts were charged and parked. Every other chain walk reads an empty parent as the top; the mirror now does
too and mirrors the chain from that top down."""
from src.domain.drive_index import DriveIndexRecord, Reason, RecordState

from .drive_mirror_fakes import AREA, USER, MirrorWorld, stored


async def _top_without_parent(w: MirrorWorld, name: str) -> str:
    """A folder the provider reports with no parent (the drive's node still sits somewhere the fake can address)."""
    item_id = w.drive.add_folder(AREA, name)
    await w.put(DriveIndexRecord(USER, item_id, parent_id="", name=name, is_folder=True,
                                 state=RecordState.METADATA_ONLY, reason=Reason.SCOPE_ANCESTOR))
    return item_id


async def test_a_recording_under_a_parentless_top_is_placed_under_that_top():
    w = MirrorWorld()
    top = await _top_without_parent(w, "Garden club")
    minutes = await w.folder("Minutes", top)
    talk = await w.recording("talk.mp4", minutes)
    counters = await w.run()
    assert counters.get("mirror_charged", 0) == 0
    assert w.mirror_path(talk) == "Garden club/Minutes/talk.mp4.md"


async def test_a_recording_with_no_visible_parent_is_placed_at_the_mirror_root():
    w = MirrorWorld()
    solo = w.drive.add_file(AREA, "solo.mp4", b"audio")
    await w.recording("unused.mp4")                     # an ordinary one in the same run
    rec = await w.put(DriveIndexRecord(USER, solo, parent_id="", name="solo.mp4", kind="audio",
                                       state=RecordState.INDEXED, effective_day="2026-10-01",
                                       transcript_state="current", transcript_key="k-solo", transcript_version=1,
                                       transcript_seconds=120, transcript_speakers=2, transcript_languages=("en",)))
    await w.repo.put_transcript(USER, stored(rec.transcript_key, "hello there"))
    counters = await w.run()
    assert counters.get("mirror_charged", 0) == 0
    assert w.mirror_path(solo) == "solo.mp4.md"


async def test_an_ancestor_with_no_record_is_still_unplaceable():
    w = MirrorWorld()
    ghost = w.drive.add_folder(AREA, "Ghost")            # on the drive, never recorded
    talk = await w.recording("talk.mp4", ghost)
    counters = await w.run()
    assert counters.get("mirror_charged", 0) == 1
    assert w.doc(talk) is None or not w.doc(talk).mirror_item_id
