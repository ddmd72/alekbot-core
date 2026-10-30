"""DriveSearchService.search_deleted reports whether any entry carries stored transcript text (a passage), so the
specialist's write gate can arm on it. Reuses the fixtures of the deleted-transcripts tests."""
from tests.unit.services.test_drive_search_deleted_transcripts import (  # noqa: F401  (fixtures)
    _doc, _stored, connections, drive, index, mirror, svc,
)


async def test_an_entry_with_a_passage_sets_the_flag(svc, mirror, index):
    await mirror.save_doc(_doc(1, content_key="k1"))
    index.get_transcript.return_value = _stored("k1", "the meeting moved to Thursday and the budget is next")
    found = await svc.search_deleted("u1", "budget")
    assert found.has_passage is True and len(found.items) == 1


async def test_hits_without_a_passage_leave_it_unset(svc, mirror):
    await mirror.save_doc(_doc(1, content_key=""))
    found = await svc.search_deleted("u1", "budget")
    assert found.has_passage is False and len(found.items) == 1


async def test_an_empty_query_with_no_stored_text_has_none(svc, mirror):
    await mirror.save_doc(_doc(1, content_key=""))
    assert (await svc.search_deleted("u1", "")).has_passage is False


async def test_no_hit_has_none(svc):
    found = await svc.search_deleted("u1", "zebra")
    assert found.has_passage is False and found.items == []
