"""search_files_in_drive's date rule after review E-M3 (USER_DRIVE_INDEXING_RFC §24.10, D13): one distinction, the same
sentence in drive_query and drive_date_from, with no per-kind subtlety (which date a scan's filter matches) in
model-facing text; the Context template names what can be searched inside."""
from src.infrastructure.agent_manifest import FILE_MANAGEMENT, Intent

RULE = ("Dates written inside the document go in drive_query; use the date filters only when the user limits when "
        "the file is dated.")
_SCHEMA = FILE_MANAGEMENT.context_schemas[Intent.SEARCH_FILES_IN_DRIVE]
_DESCRIPTION = FILE_MANAGEMENT.capability_descriptions[Intent.SEARCH_FILES_IN_DRIVE]


def test_the_rule_is_the_same_sentence_in_both_fields():
    assert _SCHEMA["drive_query"].count(RULE) == 1
    assert _SCHEMA["drive_date_from"].count(RULE) == 1


def test_no_per_kind_date_subtlety_reaches_the_model():
    dates = " ".join([_SCHEMA["drive_query"], _SCHEMA["drive_date_from"], _SCHEMA["drive_date_to"]])
    for subtlety in ("scans", "read by a model", "own date", "may stay", "doc_date"):
        assert subtlety not in dates, subtlety
    assert "doc_date" not in _DESCRIPTION and "own date" not in _DESCRIPTION


def test_the_date_filter_still_says_photos_go_by_when_they_were_taken():
    assert "when a photo was taken" in _SCHEMA["drive_date_from"]


def test_the_context_template_names_what_to_find_inside():
    template = _DESCRIPTION[_DESCRIPTION.index("Context: "):]
    assert '"drive_query": "<name words, a place or subject, or words, a number or an amount to find inside>"' in template
