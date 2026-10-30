"""A query with no name words ("???", "—") plus a filter is a filter-only request: newest first with a count,
never a kNN over a meaningless vector (USER_DRIVE_INDEXING_RFC §10.1)."""
import pytest

from src.domain.drive_search import DriveSearchRequest, request_from_fields

from .test_drive_search_service import World, paths, taken


@pytest.mark.parametrize("query", ["???", " — ", "!!! ...", "«»"])
def test_a_query_without_name_keys_is_filter_only(query):
    assert DriveSearchRequest(query=query, kind="pdf").filter_only
    assert request_from_fields(query, None, "pdf", None, None).filter_only


def test_a_query_with_name_keys_is_not():
    assert not DriveSearchRequest(query="ИП", kind="pdf").filter_only   # §25.5.1: a 2-letter word is a concept
    assert not DriveSearchRequest(query="2026").filter_only


async def test_the_service_runs_the_filter_only_path():
    w = World()
    w.drive.add("old.pdf", media=taken("2025-01-01"))
    w.drive.add("new.pdf", media=taken("2026-01-01"))
    await w.settle()
    calls = len(w.emb.calls)
    result = await w.search("???", kind="pdf")
    assert paths(result) == ["new.pdf", "old.pdf"] and result.matched_total == 2
    assert len(w.emb.calls) == calls and w.repo.card_limits == []
