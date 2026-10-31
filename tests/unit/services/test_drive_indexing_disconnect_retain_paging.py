"""I8 at scale (MULTI_DRIVE_RFC §10, E23): a disconnect's purge retains the stored transcripts of a connection holding
more records than one retention page, the cursor carrying the purge from page to page. Technical ids only."""
from dataclasses import replace

from src.domain.drive_transcript import RETAINED_DISCONNECT, StoredTranscript
from src.services import drive_indexing_service as indexing_mod

from .drive_multi_connection_world import USER, MultiWorld


async def test_the_retention_pages_through_records_and_marks_every_head(monkeypatch):
    monkeypatch.setattr(indexing_mod, "RECORD_WRITE_BATCH", 2)      # three records: two pages
    w = MultiWorld()
    await w.connect("conn-1", ref_tag="a1", drive_identity="fake_a:drive-1")
    for n in (1, 2, 3):
        w.drives["conn-1"].add(f"data{n}.bin", item_id=f"item-{n}")
    await w.settle("conn-1")
    records = w.records("conn-1")
    assert sorted(records) == ["item-1", "item-2", "item-3"]
    await w.repo.upsert_records([replace(records[f"item-{n}"], transcript_key=f"fam:d:{n}") for n in (1, 2, 3)])
    for key in ("fam:d:1", "fam:d:2", "fam:d:3", "fam:d:4"):
        await w.repo.put_transcript(USER, StoredTranscript(content_key=key, transcript=None, last_used_at=w.clock.now))
    await w.connections.disconnect(USER, "conn-1")
    assert (await w.settle("conn-1")).status == "purged"
    assert w.records("conn-1") == {}
    for key in ("fam:d:1", "fam:d:2", "fam:d:3"):
        kept = await w.repo.get_transcript(USER, key)
        assert (kept.retained_at, kept.retained_reason) == (w.clock.now, RETAINED_DISCONNECT)
    assert (await w.repo.get_transcript(USER, "fam:d:4")).retained_at is None
