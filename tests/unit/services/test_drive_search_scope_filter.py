"""While a narrowed scope is being re-indexed the index may still hold what the scope change took out: search,
find_passages and read_passages filter by `in_scope` over the index's own chain (design P-C addendum, review R1 N2)."""
from dataclasses import replace

from src.domain import drive_ref
from src.domain.drive_capabilities import DriveCapabilities
from src.domain.drive_excerpt_range_request import ExcerptRangeRequest
from src.domain.drive_excerpt_request import ExcerptRequest

from .drive_two_drives_harness import TwoDrives

QUESTION = "how does the review process work"


async def _world(scope=("Fin",), whole=False) -> TwoDrives:
    w = TwoDrives(acme=False)
    w.primary = replace(w.primary, scope_folder_ids=scope, scope_whole_drive=whole)
    calls = []

    async def capabilities(handle):
        calls.append(handle)
        return DriveCapabilities(max_path_chars=400)
    w.drive.capabilities = capabilities
    w.emb.pinned[QUESTION] = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    await w.open()
    inside = w.folder("Coimbra 2024", item="Fin")
    outside = w.folder("Springfield", item="Fout")
    w.kept = w.file("Tom's plan.docx", kind="docx", parent=inside, chunks=["The review is held weekly."])
    w.dropped = w.file("Old plan.docx", kind="docx", parent=outside, chunks=["The review was held monthly."])
    return w


async def test_search_does_not_return_a_record_under_a_folder_taken_out_of_scope():
    w = await _world()
    result = await w.find("plan")
    assert [c.item_id for c in result.cards] == [w.kept]


async def test_find_passages_and_read_passages_leave_it_out_too():
    w = await _world()
    found = await w.service.excerpts("u", ExcerptRequest(question=QUESTION))
    assert {f.item_id for f in found.files} == {w.kept}
    ranged = await w.service.excerpt_range("u", ExcerptRangeRequest(drive_ref.format("a1", w.dropped), 1, 1))
    assert not ranged.files


async def test_the_whole_drive_scope_filters_nothing():
    w = await _world(scope=(), whole=True)
    result = await w.find("plan")
    assert {c.item_id for c in result.cards} == {w.kept, w.dropped}
