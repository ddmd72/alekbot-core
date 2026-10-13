"""What found each card is shown to Smart (USER_DRIVE_INDEXING_RFC §10.2, final review F5-8).

`matched` was computed but never rendered, and every content hit's window was labelled "matching passage" even when
only the chunk kNN found it (the passage is the nearest one, not one holding the user's words). Smart's folder rule
applies only to a folder its name matched; a folder card near in meaning is not a lead to search inside."""
from src.domain.drive_search import render_results

from .test_drive_search_content_streams import FAR, admit, by_id, index, query_vector, settle_metadata, world  # noqa: F401


async def test_a_name_match_says_so(monkeypatch):
    w = world()
    w.drive.add("Contracts", folder=True)
    await settle_metadata(w, monkeypatch)
    result = await w.search("contracts")
    assert by_id(result) and "matched: name" in render_results(result)


async def test_filter_only_results_carry_no_matched_line(monkeypatch):
    w = world()
    w.drive.add("Contracts", folder=True)
    await settle_metadata(w, monkeypatch)
    result = await w.search("", kind="folder")
    assert result.cards and "matched:" not in render_results(result)
