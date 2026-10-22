"""A drive search with nothing to search for is refused with a correction, never answered with "the newest files"
(USER_DRIVE_INDEXING_RFC §10.1, §10.4). Since the cut the search is DriveOperations.search, called by the specialist's
`search_drive` with its own `query`/`folder`/`kind`/`date_*` parameters (DRIVE_SPECIALIST_RFC §7.6, §8.1); the old
payloads are mapped field by field, and Smart's delegate `query` (never read for search) has no counterpart."""
from unittest.mock import AsyncMock

import pytest

from src.domain.drive_search import STATUS_OK, DriveSearchResult
from src.infrastructure.drive_operations import DriveOperations
from src.services.drive_search_service import DriveSearchService
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService


_FIELDS = {"drive_query": "query", "drive_folder": "folder", "drive_kind": "kind", "drive_date_from": "date_from",
           "drive_date_to": "date_to"}


def _search(ops, payload):
    """The old payload as search_drive's arguments: each drive_* field to its parameter."""
    return ops.search({"user_id": "u1", "account_id": "a1"}, "u1",
                      **{new: payload[old] for old, new in _FIELDS.items() if old in payload})


@pytest.fixture
def search():
    svc = AsyncMock(spec=DriveSearchService)
    svc.search.return_value = DriveSearchResult(status=STATUS_OK, matched_total=0)
    return svc


def _agent(search):
    return DriveOperations(conversion_service=AsyncMock(spec=FileConversionService),
                           drive_service=AsyncMock(spec=UserDriveService), drive_search=search)


@pytest.mark.parametrize("payload", [
    {"query": "find the lease contract"},
    {"query": "find the lease contract", "drive_query": "  "},
    {"drive_query": "???"},
    {"drive_query": "", "drive_folder": "/"},
])
async def test_nothing_to_search_for_is_refused_with_the_correction(search, payload):
    resp = await _search(_agent(search), payload)
    assert not resp.success
    assert "in query" in resp.text and "date_from" in resp.text and "filter" in resp.text
    search.search.assert_not_called()


@pytest.mark.parametrize("payload", [
    {"drive_kind": "spreadsheet"}, {"drive_folder": "Contracts"}, {"drive_date_from": "2026-10-01"},
    {"drive_date_to": "2026-10-01"}, {"drive_query": "lease"}, {"drive_query": "Rome"},
])
async def test_name_words_or_any_filter_is_enough(search, payload):
    resp = await _search(_agent(search), payload)
    assert resp.success and search.search.await_count == 1


def test_the_query_field_description_says_it_is_required_and_what_goes_in_it():
    # Reviewer edit at the cut (DRIVE_SPECIALIST_RFC §7.6, §8.1): the field is the specialist's `search_drive.query`;
    # field names become parameter names, and the intent prefix and the "`query` field is not read" clause go (§7.6).
    # Today's intent sentence on filter-only requests moves to the specialist's PROTOCOL_DRIVE_SEARCH, renamed.
    from pathlib import Path
    from src.domain import drive_tool_texts as tt
    text = tt.QUERY
    assert "REQUIRED unless the request is filter-only" in text
    assert "is not read" not in text and "search_files_in_drive" not in text
    # Search v2 (D25, §25.13.6): document types are allowed, a file format goes in kind and a time span in
    # the date fields (v1's "no type words" and "dates written inside go in query" are reversed by D25)
    assert "a place, a person, a topic, a document type or purpose" in text
    assert "A file format goes in kind" in text
    assert "goes in date_from / date_to, not in the words" in text
    assert "no type words" not in text.lower() and "Dates written inside the document go in query" not in text
    token = (Path(__file__).resolve().parents[3] / "prompts_snapshot" / "tokens" / "system"
             / "PROTOCOL_DRIVE_SEARCH.groovy").read_text(encoding="utf-8").split("\n---\n", 1)[1]
    assert "query is required unless the request is filter-only" in token


def test_the_eval_refuses_what_the_agent_refuses():
    from scripts.validation.drive_eval.runner import request_for
    with pytest.raises(ValueError, match="in query, or set a filter"):
        request_for({"query": "", "filters": {}})
    assert request_for({"query": "", "filters": {"kind": "pdf"}}).filter_only
