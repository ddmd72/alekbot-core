"""The document service's contract (EXTERNAL_FILES_RFC §4.4): the statuses, the request, the outcome's neutral defaults,
the deadlines and the kind sets. The last test ties PARSE_KINDS to the parser child's own reader table."""
import dataclasses

import pytest

from src.adapters.drive_parser import readers
from src.domain.document_extraction import ExtractionStatus
from src.domain.document_read import (
    CALLER_OPEN, CALLER_PREFETCH, IN_PROCESS_TEXT_KINDS, INTERACTIVE_OPEN_S, OPEN_MARGIN_S, PARSE_KINDS,
    PREFETCH_OPEN_S, TRANSCRIPT_CACHE_DAYS, ReadOutcome, ReadRequest, ReadStatus,
)
from src.domain.native_format import NativeFormat


def test_the_eight_per_file_statuses_carry_the_extraction_strings():
    """The status map (§4.5) is one to one: the shared values are the same strings."""
    shared = {s.value for s in ExtractionStatus} - {ExtractionStatus.BUDGET_EXHAUSTED.value}
    assert shared <= {s.value for s in ReadStatus}
    for value in shared:
        assert ReadStatus(value).value == ExtractionStatus(value).value


def test_the_four_statuses_of_the_service():
    assert {s.value for s in ReadStatus} - {s.value for s in ExtractionStatus} == {
        "native", "model_unavailable", "parser_unavailable", "still_working"}
    assert not hasattr(ReadStatus, "BUDGET_EXHAUSTED")    # P13: unreachable from the service


def test_an_outcome_needs_only_its_status():
    outcome = ReadOutcome(status=ReadStatus.ERROR)
    assert (outcome.kind, outcome.native_mime, outcome.text, outcome.detail) == ("", "", "", "")
    assert (outcome.pages_total, outcome.pages_read, outcome.model_calls, outcome.model_cost_usd) == (0, 0, 0, 0.0)
    assert (outcome.skipped_pages, outcome.links, outcome.cut_within_pages) == ((), (), ())
    assert outcome.truncated is False and outcome.may_be_incomplete is False


def test_the_outcome_fields_are_the_rfcs_plus_the_cut_window():
    assert [f.name for f in dataclasses.fields(ReadOutcome)] == [
        "status", "kind", "native_mime", "text", "pages_total", "pages_read", "skipped_pages", "truncated",
        "may_be_incomplete", "links", "model_calls", "model_cost_usd", "detail", "cut_within_pages"]


def test_a_request_is_plain_values_and_frozen():
    request = ReadRequest(content=b"%PDF-1.7", name="scan_0012.pdf", declared_mime="application/pdf", store="chat",
                          caller=CALLER_OPEN, native_formats=(NativeFormat("application/pdf", 1024, 100),),
                          user_id="ann", account_id="ann", deadline_at=1_000.0, request_id="r1")
    assert [f.name for f in dataclasses.fields(ReadRequest)] == [
        "content", "name", "declared_mime", "store", "caller", "native_formats", "user_id", "account_id",
        "deadline_at", "request_id"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.name = "invoice_painting_es.pdf"  # type: ignore[misc]


def test_the_deadlines():
    assert OPEN_MARGIN_S == 30.0
    assert INTERACTIVE_OPEN_S == 90.0           # FileManagement's 120 s minus the margin
    assert PREFETCH_OPEN_S == 270.0             # under Cloud Tasks' 600 s default dispatch deadline
    assert PREFETCH_OPEN_S + OPEN_MARGIN_S < 600


def test_the_callers_and_the_cache_life():
    assert (CALLER_OPEN, CALLER_PREFETCH) == ("open", "prefetch")
    assert TRANSCRIPT_CACHE_DAYS == 90


def test_in_process_kinds_are_never_parse_kinds():
    assert IN_PROCESS_TEXT_KINDS == {"text", "markdown", "json", "code", "config", "log"}   # config: policy 5 (Δ248)
    assert not IN_PROCESS_TEXT_KINDS & PARSE_KINDS


def test_parse_kinds_are_the_child_readers_without_the_in_process_ones():
    """A reader added to (or removed from) the child must be placed on one side or the other."""
    assert PARSE_KINDS | {"text", "markdown", "json", "config"} == set(readers._READERS)
