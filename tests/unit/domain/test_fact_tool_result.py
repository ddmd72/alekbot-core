"""M3: the words of the fact tool result live in `domain.fact_tool_result`; the readers use them, not literals."""
import pytest

from src.domain.consolidation_pass_stats import OUTCOME_BORN_DUE, OUTCOME_ERROR, OUTCOME_EXPIRY, OUTCOME_OK
from src.domain.fact_tool_result import (
    NOTE_NO_READABLE_SOURCE,
    REFUSED_EXPIRY,
    STATUS_CREATED,
    STATUS_FAILED,
    STATUS_MERGED,
    STATUS_NOT_FOUND,
    STATUS_UPDATED,
    WRITTEN_STATUSES,
    expiry_refusal_tags,
    note_codes_of,
    outcome_of,
    with_notes,
)
from src.domain.fact_write_context import NO_READABLE_SOURCE_NOTE


def test_the_words_are_the_strings_the_results_carry():
    assert (STATUS_CREATED, STATUS_UPDATED, STATUS_MERGED, STATUS_FAILED, STATUS_NOT_FOUND) == (
        "created", "updated", "merged", "failed", "not_found")
    assert WRITTEN_STATUSES == {"created", "updated", "merged"}
    assert REFUSED_EXPIRY == "expiry" and NOTE_NO_READABLE_SOURCE == "no_readable_source"


@pytest.mark.parametrize("result, expected", [
    ("not a dict", OUTCOME_OK),
    (None, OUTCOME_OK),
    ({"status": STATUS_FAILED, "refused": REFUSED_EXPIRY}, OUTCOME_EXPIRY),
    ({"status": STATUS_FAILED, "refused": REFUSED_EXPIRY, "born_due": True}, OUTCOME_BORN_DUE),
    ({"status": STATUS_FAILED, "message": "boom"}, OUTCOME_ERROR),
    ({"status": STATUS_NOT_FOUND}, OUTCOME_ERROR),
    ({"error": "boom"}, OUTCOME_ERROR),
    ({"status": STATUS_CREATED, "fact_id": "f1"}, OUTCOME_OK),
    ({"status": STATUS_UPDATED}, OUTCOME_OK),
    ({"results": []}, OUTCOME_OK),
], ids=["non-dict", "none", "expiry", "born-due", "failed", "not-found", "error-key", "created", "updated", "search"])
def test_outcome_of_reads_every_branch(result, expected):
    assert outcome_of(result) == expected


def test_a_refusal_outranks_a_failed_status():
    assert outcome_of({"status": STATUS_FAILED, "refused": "something else"}) == OUTCOME_ERROR


@pytest.mark.parametrize("born_due, expected", [
    (False, {"refused": "expiry"}),
    (True, {"refused": "expiry", "born_due": True}),
])
def test_expiry_refusal_tags(born_due, expected):
    assert expiry_refusal_tags(born_due) == expected


def test_no_notes_leave_the_result_untouched():
    result = {"status": STATUS_CREATED}
    assert with_notes(result, []) == {"status": "created"}


def test_an_uncoded_note_is_joined_without_a_code():
    assert with_notes({"status": STATUS_CREATED}, ["expiry ignored"]) == {"status": "created", "note": "expiry ignored"}


def test_the_no_readable_source_note_carries_its_code():
    result = with_notes({"status": STATUS_CREATED}, [NO_READABLE_SOURCE_NOTE])
    assert result == {"status": "created", "note": NO_READABLE_SOURCE_NOTE, "note_codes": ["no_readable_source"]}


def test_several_notes_are_joined_and_only_the_known_ones_coded():
    result = with_notes({}, ["first", NO_READABLE_SOURCE_NOTE, "last"])
    assert result["note"] == f"first; {NO_READABLE_SOURCE_NOTE}; last"
    assert result["note_codes"] == ["no_readable_source"]


def test_note_codes_match_by_equality_not_by_substring():
    assert note_codes_of([NO_READABLE_SOURCE_NOTE + " (extended)", "a " + NO_READABLE_SOURCE_NOTE]) == []
