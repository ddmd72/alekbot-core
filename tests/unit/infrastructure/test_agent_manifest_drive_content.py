"""search_files_in_drive answers from content (USER_DRIVE_INDEXING_RFC §24.10, D11, D13; §17.7).

The intent stays as it is; its description and the drive_query field tell Smart that content questions go into
drive_query (a subject, a phrase, a number, an amount), and the date fields say which date a filter matches. The
user-facing capabilities text says the drive answers from content and reads scans."""
from src.infrastructure.agent_manifest import FILE_MANAGEMENT, Intent
from src.utils.capabilities import CAPABILITIES_TEXT

_SCHEMA = FILE_MANAGEMENT.context_schemas[Intent.SEARCH_FILES_IN_DRIVE]
_DESCRIPTION = FILE_MANAGEMENT.capability_descriptions[Intent.SEARCH_FILES_IN_DRIVE]


def test_the_description_offers_content_search():
    for phrase in ("what is written inside", "scans", "screenshots", "a phrase", "a number or an amount",
                   "the passage it matched"):
        assert phrase in _DESCRIPTION, phrase


def test_content_words_go_into_drive_query():
    text = _SCHEMA["drive_query"]
    assert "what to find inside the documents" in text
    for example in ("'notice period'", "'INV-2024-417'", "'1 250,00'"):
        assert example in text, example
    # review E-M3 / E-Δ2: one date distinction instead of "a date the user quotes ... may stay"
    assert ("Dates written inside the document go in drive_query; use the date filters only when the user limits "
            "when the file is dated.") in text


def test_the_date_filter_says_which_date_it_matches():
    text = _SCHEMA["drive_date_from"]
    # review E-M3: no per-kind date subtlety (scans' own date) in model-facing text; the filter says photos go by
    # when they were taken and everything else by when the file is dated
    assert "The date is when a photo was taken, otherwise when the file is dated." in text
    assert "own date" not in text and "scans" not in text


def test_capabilities_say_the_drive_answers_from_content():
    assert "I answer from what is written in your documents" in CAPABILITIES_TEXT
    assert "I read scans, screenshots and photographed receipts" in CAPABILITIES_TEXT
    assert "not what is inside the files" not in CAPABILITIES_TEXT
