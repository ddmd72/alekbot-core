"""`DriveOperations.find_passages` / `.read_passages` (USER_DRIVE_INDEXING_RFC §28.3 A3, A5, A7, A10, A11, A18; plan
S2.1): one call to the search service's passages methods, the rendered text, the typed result and the context plan
for the run's ledger, the files for `drive_context`, the facts, the outcome class of every gate state, the request
errors, and the open path's `opened:` log line (§28.7). The search service is a spec mock (its own behaviour is
covered by its tests); results are built by hand from the invented universe."""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.drive_excerpt_context import ExcerptContext, folder_line
from src.domain.drive_excerpt_file_context import FileContext
from src.domain.drive_excerpt_folder_note import FolderNote
from src.domain.drive_excerpt_range_request import ExcerptRangeRequest
from src.domain.drive_excerpt_request import ExcerptRequest
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_search import (
    STATUS_AMBIGUOUS_FOLDER, STATUS_DISABLED, STATUS_DISCONNECTED, STATUS_FOLDER_NOT_FOUND, STATUS_OK,
    DriveSearchRequest, DriveSearchResult,
)
from src.domain.user_drive import DriveItem
from src.infrastructure.drive_operations import NO_SEARCH, DriveOperations
from src.services.drive_search_service import DriveSearchService
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_service_connections import ConnectionReads
from tests.unit.domain.drive_excerpt_builders import LEASE, TOMS_PLAN, chunk, excerpt_file, result

CTX = {"user_id": "u1", "account_id": "a1"}
QUESTION = "What does Tom's plan say happens after the first review?"
PLAN = excerpt_file("item-1", TOMS_PLAN, [chunk("item-1", 2, "After the first review, Acme signs off.")])
LEASE_FILE = excerpt_file("item-2", LEASE, [chunk("item-2", 0, "The rent is due on the first day.")], kind="pdf")
RD_NOTE = "noted by the owner: «Acme specifications; the review process lives here»"
RD = FolderNote("f-rd", "R&D", 1, RD_NOTE)
NOTED = ExcerptContext(files=(FileContext("item-1", TOMS_PLAN, ("f-rd",)),), folders={"f-rd": RD})


def _ops(search=None):
    drive = AsyncMock(spec=UserDriveService)
    return DriveOperations(conversion_service=MagicMock(spec=FileConversionService), drive_service=drive,
                           drive_search=search)


def _search(found=None, ranged=None):
    search = AsyncMock(spec=DriveSearchService)
    search.excerpts.return_value = found if found is not None else result(PLAN, question=QUESTION, ref_tag="a1")
    search.excerpt_range.return_value = ranged if ranged is not None else result(
        PLAN, request=ExcerptRangeRequest("drive:item-1", 3, 3), ref_tag="a1")
    return search


# -- find_passages -------------------------------------------------------------------------------------------------

async def test_a_find_returns_the_rendered_passages_with_its_typed_result_and_files():
    found = result(PLAN, LEASE_FILE, question=QUESTION, ref_tag="a1")
    search = _search(found)
    out = await _ops(search).find_passages(CTX, "u1", question=f"  {QUESTION}\n", folder="R&D")
    search.excerpts.assert_awaited_once_with("u1", ExcerptRequest(question=QUESTION, folder="R&D"),
                                             shown=frozenset())
    assert out.op is DriveOp.PASSAGES and out.cls is OutcomeClass.OK and out.success
    assert "[id drive:a1:item-1:3-3 · " in out.text and "After the first review, Acme signs off." in out.text
    assert "The rent is due on the first day." in out.text
    assert out.excerpts is found and out.excerpt_plan is not None
    assert [i.item_id for i in out.items] == ["item-1", "item-2"]
    assert out.items[0] == PLAN.drive_item()
    assert out.drive_context() == [{"path": TOMS_PLAN, "ref": "drive:a1:item-1"},
                                   {"path": LEASE, "ref": "drive:a1:item-2"}]
    assert out.facts.excerpts is True and out.request_text == QUESTION
    assert out.body is False   # the specialist's model reads passages in full (A2)


async def test_a_find_passes_the_ledgers_chunks_to_the_service_and_its_context_sets_to_the_render():
    """Three sets on a find: `shown` to the service (new chunks only), the folders shown and files described to the
    render, so a noted folder already shown prints no line again."""
    found = result(PLAN, question=QUESTION, context=NOTED, ref_tag="a1")
    search = _search(found)
    shown = frozenset({("item-9", 1, 0)})
    fresh = await _ops(search).find_passages(CTX, "u1", question=QUESTION)
    again = await _ops(search).find_passages(CTX, "u1", question=QUESTION, shown=shown,
                                             shown_folders=frozenset({"f-rd"}), described_files=frozenset({"item-1"}))
    assert search.excerpts.await_args_list[1].kwargs == {"shown": shown}
    assert folder_line(RD) in fresh.text and folder_line(RD) not in again.text
    assert fresh.excerpt_plan.shown_after == frozenset({"f-rd"})


async def test_owner_note_is_set_exactly_when_the_plan_printed_an_owner_line():
    search = _search(result(PLAN, question=QUESTION, context=NOTED, ref_tag="a1"))
    printed = await _ops(search).find_passages(CTX, "u1", question=QUESTION)
    shown_before = await _ops(search).find_passages(CTX, "u1", question=QUESTION, shown_folders=frozenset({"f-rd"}))
    plain = await _ops(_search(result(PLAN, question=QUESTION, ref_tag="a1"))).find_passages(CTX, "u1", question=QUESTION)
    assert printed.excerpt_plan.has_owner_line and printed.facts.owner_note is True
    assert not shown_before.excerpt_plan.has_owner_line and shown_before.facts.owner_note is False
    assert plain.facts.owner_note is False


async def test_a_find_with_no_passage_is_not_found_and_reads_no_content():
    out = await _ops(_search(result(question=QUESTION))).find_passages(CTX, "u1", question=QUESTION)
    assert out.cls is OutcomeClass.NOT_FOUND and out.success
    assert out.facts.excerpts is False and out.items == () and "No indexed passage matched" in out.text


@pytest.mark.parametrize("fields, building", [
    ({"building": True}, True), ({"building": True, "awaiting_rebuild": True}, True), ({}, False)])
async def test_a_find_during_a_build_reports_the_building_fact_as_search_does(fields, building):
    """An empty find while the index is first built must not read as "no such document" (R_BUILDING)."""
    out = await _ops(_search(result(question=QUESTION, **fields))).find_passages(CTX, "u1", question=QUESTION)
    assert out.facts.building is building and out.facts.excerpts is False


async def test_file_refs_given_as_a_list_or_a_comma_string_reach_the_service_normalised():
    search = _search()
    await _ops(search).find_passages(CTX, "u1", question=QUESTION, file_refs="drive:item-1, drive:item-2")
    await _ops(search).find_passages(CTX, "u1", question=QUESTION, file_refs=["drive:item-1"])
    requests = [c.args[1] for c in search.excerpts.await_args_list]
    assert [r.file_refs for r in requests] == [("drive:item-1", "drive:item-2"), ("drive:item-1",)]


@pytest.mark.parametrize("kwargs", [
    {"question": ""}, {"question": "   "}, {"question": None},
    {"question": QUESTION, "file_refs": "R&D/Tom's plan.docx"},
    {"question": QUESTION, "file_refs": ",".join(f"drive:i{n}" for n in range(11))},
])
async def test_a_find_request_error_is_correctable_and_calls_no_service(kwargs):
    search = _search()
    out = await _ops(search).find_passages(CTX, "u1", **kwargs)
    search.excerpts.assert_not_called()
    assert out.cls is OutcomeClass.REQUEST_ERROR and not out.success and out.text
    assert out.excerpts is None


# -- read_passages -------------------------------------------------------------------------------------------------

async def test_a_range_read_returns_the_files_passages_with_no_request_text():
    ranged = result(PLAN, request=ExcerptRangeRequest("drive:item-1", 3, 4), context=NOTED, ref_tag="a1")
    search = _search(ranged=ranged)
    out = await _ops(search).read_passages(CTX, "u1", file_ref="drive:item-1", passages="3–4",
                                           shown_folders=frozenset({"f-rd"}))
    search.excerpt_range.assert_awaited_once_with("u1", ExcerptRangeRequest("drive:item-1", 3, 4))
    assert out.op is DriveOp.PASSAGES and out.cls is OutcomeClass.OK and out.success
    assert "Passages read by number" in out.text and "After the first review, Acme signs off." in out.text
    assert folder_line(RD) not in out.text   # the folder already shown in this run
    assert out.excerpts is ranged and out.facts.excerpts is True and out.request_text == ""
    assert [i.item_id for i in out.items] == ["item-1"]


async def test_a_range_read_that_is_cut_says_where_it_continues():
    ranged = result(PLAN, request=ExcerptRangeRequest("drive:item-1", 3, 12), continue_at=4, ref_tag="a1")
    out = await _ops(_search(ranged=ranged)).read_passages(CTX, "u1", file_ref="drive:item-1", passages="3-12")
    assert "read_passages from passage 4 continues" in out.text


@pytest.mark.parametrize("file_ref, passages", [
    ("", "3"), (None, "3"), ("Tom's plan.docx", "3"), ("drive:item-1", ""), ("drive:item-1", "five"),
    ("drive:item-1", "5-3"), ("drive:item-1", "1-11"), ("drive:item-1", "0"),
])
async def test_a_range_request_error_is_correctable_and_calls_no_service(file_ref, passages):
    search = _search()
    out = await _ops(search).read_passages(CTX, "u1", file_ref=file_ref, passages=passages)
    search.excerpt_range.assert_not_called()
    assert out.cls is OutcomeClass.REQUEST_ERROR and not out.success and out.text


# -- the outcome class of every state (§7.2) -------------------------------------------------------------------------

@pytest.mark.parametrize("fields, cls", [
    ({"status": STATUS_OK, "awaiting_rebuild": True, "building": True}, OutcomeClass.REBUILDING),
    ({"status": STATUS_OK, "wrong_space": True}, OutcomeClass.REBUILDING),
    ({"status": STATUS_DISCONNECTED, "other_drive": True}, OutcomeClass.REBUILDING),
    ({"status": STATUS_DISABLED}, OutcomeClass.INDEXING_OFF),
    ({"status": STATUS_DISCONNECTED}, OutcomeClass.NOT_CONNECTED),
    ({"status": STATUS_FOLDER_NOT_FOUND}, OutcomeClass.NOT_FOUND),
    ({"status": STATUS_AMBIGUOUS_FOLDER, "folder_candidates": ("R&D", "Archive/R&D")}, OutcomeClass.OK),
    ({"status": STATUS_OK, "folder_not_indexed": True, "folder_path": "Code"}, OutcomeClass.NOT_FOUND),
    ({"status": STATUS_OK, "building": True}, OutcomeClass.NOT_FOUND),
    ({"status": STATUS_OK}, OutcomeClass.NOT_FOUND),
])
async def test_each_state_of_the_result_has_its_outcome_class(fields, cls):
    found = result(request=ExcerptRequest(question=QUESTION, folder="R&D"), **fields)
    out = await _ops(_search(found)).find_passages(CTX, "u1", question=QUESTION, folder="R&D")
    assert out.cls is cls and out.success and out.text and out.facts.excerpts is False


async def test_files_make_a_find_ok_whatever_else_it_says():
    found = result(PLAN, question=QUESTION, building=True, no_text=2, no_text_paths=("a.pdf", "b.pdf"),
                   ref_tag="a1")
    out = await _ops(_search(found)).find_passages(CTX, "u1", question=QUESTION)
    assert out.cls is OutcomeClass.OK and "2 files in scope have no indexed text" in out.text


async def test_an_ambiguous_folder_shows_its_candidates():
    found = result(request=ExcerptRequest(question=QUESTION, folder="R&D"), status=STATUS_AMBIGUOUS_FOLDER,
                   folder_candidates=("R&D", "Archive/R&D"))
    out = await _ops(_search(found)).find_passages(CTX, "u1", question=QUESTION, folder="R&D")
    assert '"R&D", "Archive/R&D"' in out.text


# -- no search service, a failing one, no drive ----------------------------------------------------------------------

async def test_without_a_search_service_both_tools_fail_with_todays_line():
    ops = _ops(None)
    find = await ops.find_passages(CTX, "u1", question=QUESTION)
    read = await ops.read_passages(CTX, "u1", file_ref="drive:item-1", passages="1")
    assert [(o.cls, o.text, o.op) for o in (find, read)] == [(OutcomeClass.FAILED, NO_SEARCH, DriveOp.PASSAGES)] * 2


async def test_a_service_error_is_named_not_leaked():
    search = _search()
    search.excerpts.side_effect = RuntimeError("secret detail")
    out = await _ops(search).find_passages(CTX, "u1", question=QUESTION)
    assert out.cls is OutcomeClass.FAILED and out.text == "Drive operation failed: RuntimeError."


async def test_with_no_drive_wired_the_tools_answer_not_configured():
    ops = DriveOperations(conversion_service=MagicMock(spec=FileConversionService), drive_search=_search())
    out = await ops.find_passages(CTX, "u1", question=QUESTION)
    assert out.cls is OutcomeClass.FAILED and out.text == "The drive is not configured."


# -- search and open: what the Searched line and §28.7 read -----------------------------------------------------------

async def test_a_search_carries_its_query_as_request_text():
    search = AsyncMock(spec=DriveSearchService)
    search.search.return_value = DriveSearchResult(status=STATUS_OK, request=DriveSearchRequest(query="Tom's plan"))
    out = await _ops(search).search(CTX, "u1", query="Tom's plan")
    assert out.request_text == "Tom's plan"


class _Reads(ConnectionReads):
    pass


def _drive_with(item):
    """The drive service mock: refs resolve to the seeded connection, the item is read by handle (an open reads it
    through UserDriveService.get_item, MULTI_DRIVE_RFC §6)."""
    drive = AsyncMock(spec=UserDriveService)
    drive.resolve_ref.side_effect = _Reads().resolve_ref
    drive.get_item.return_value = item
    return drive


async def test_an_open_logs_the_ref_and_the_size_of_the_converted_text(caplog):
    item = DriveItem(item_id="item-1", name="Tom's plan.docx", path=TOMS_PLAN, is_folder=False, size_bytes=4096,
                     mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    conversion = AsyncMock(spec=FileConversionService)
    conversion.resolve_content.return_value = "x" * 1234
    search = AsyncMock(spec=DriveSearchService)
    search.archived_text.return_value = None
    ops = DriveOperations(conversion_service=conversion, drive_service=_drive_with(item), drive_search=search)
    with caplog.at_level(logging.INFO):
        out = await ops.open(CTX, "u1", "drive:a1:item-1")
    assert out.cls is OutcomeClass.OK and out.body
    assert "[DriveOperations] opened: drive:a1:item-1 chars=1234" in [r.getMessage() for r in caplog.records]


async def test_an_open_that_converts_nothing_logs_no_opened_line(caplog):
    item = DriveItem(item_id="item-3", name="photo.heic", path="Photos/photo.heic", is_folder=False,
                     mime_type="image/heic")
    conversion = AsyncMock(spec=FileConversionService)
    search = AsyncMock(spec=DriveSearchService)
    search.archived_text.return_value = None
    ops = DriveOperations(conversion_service=conversion, drive_service=_drive_with(item), drive_search=search)
    with caplog.at_level(logging.INFO):
        await ops.open(CTX, "u1", "drive:a1:item-3")
    assert not [r for r in caplog.records if r.getMessage().startswith("[DriveOperations] opened:")]
