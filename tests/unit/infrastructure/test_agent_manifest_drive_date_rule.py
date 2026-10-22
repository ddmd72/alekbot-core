"""search_files_in_drive's date rule (USER_DRIVE_INDEXING_RFC §24.10, D13; Search v2 D25, §25.5.7, §25.13.6): a time
span goes in the date fields, both date fields say the same thing about which date they match, with no per-kind
subtlety (which date a scan's filter matches) in model-facing text; the Context template names what can be searched
inside."""
from src.domain import drive_tool_texts as tt
from src.domain.drive_tool_texts import drive_tool_declarations

# Retargeted at the cut (DRIVE_SPECIALIST_RFC §7.6, §8.1): the search fields are the drive specialist's `search_drive`
# parameters now, and the intent's description is that tool's description.
_SCHEMA = {"drive_query": tt.QUERY, "drive_date_from": tt.DATE_FROM, "drive_date_to": tt.DATE_TO}
_DESCRIPTION = next(t["description"] for t in drive_tool_declarations() if t["name"] == tt.SEARCH_DRIVE)

RULE = ("a photo's taken date, otherwise the date written in the document when the index found one, otherwise the "
        "file's date. Use it for a time span the user gives.")


def test_the_rule_is_the_same_sentence_in_both_fields():
    # D25: the span itself is sent from drive_query to the date fields, and both date fields name the same date
    assert "goes in date_from / date_to, not in the words" in _SCHEMA["drive_query"]
    assert _SCHEMA["drive_date_from"].count(RULE) == 1
    assert _SCHEMA["drive_date_to"].count(RULE) == 1


def test_no_per_kind_date_subtlety_reaches_the_model():
    dates = " ".join([_SCHEMA["drive_query"], _SCHEMA["drive_date_from"], _SCHEMA["drive_date_to"]])
    for subtlety in ("scans", "read by a model", "own date", "may stay", "doc_date"):
        assert subtlety not in dates, subtlety
    assert "doc_date" not in _DESCRIPTION and "own date" not in _DESCRIPTION


def test_the_date_filter_still_says_photos_go_by_when_they_were_taken():
    assert "a photo's taken date" in _SCHEMA["drive_date_from"]


def test_the_context_template_names_what_to_find_inside():
    template = _DESCRIPTION[_DESCRIPTION.index("Context: "):]
    assert ('"drive_query": "<the subject, a document type or purpose, or words, a number or an amount to find '
            'inside>"') in template
