"""search_files_in_drive answers from content (USER_DRIVE_INDEXING_RFC §24.10, D11, D13; §17.7; Search v2 D25,
§25.13.6).

The intent stays as it is; its description and the drive_query field tell Smart that content questions go into
drive_query (a subject, a phrase, a number, an amount, an id), and the date fields say which date a filter matches.
The user-facing capabilities text says the drive answers from content and reads scans."""
from src.infrastructure.agent_manifest import FILE_MANAGEMENT, Intent
from src.utils.capabilities import CAPABILITIES_TEXT

_SCHEMA = FILE_MANAGEMENT.context_schemas[Intent.SEARCH_FILES_IN_DRIVE]
_DESCRIPTION = FILE_MANAGEMENT.capability_descriptions[Intent.SEARCH_FILES_IN_DRIVE]


def test_the_description_offers_content_search():
    for phrase in ("what is written inside", "scans", "screenshots", "a phrase", "a number or an amount",
                   "for a passage match, the passage"):
        assert phrase in _DESCRIPTION, phrase


def test_content_words_go_into_drive_query():
    text = _SCHEMA["drive_query"]
    assert "or what to find inside (a phrase, a number, an amount, an id)" in text
    for example in ("'tickets'", "'receipts'", "'contracts'", "'what expires'"):   # D25: document types allowed
        assert example in text, example
    # D25 replaces review E-M3's rule: a time span goes in the date fields, which match the date written in the
    # document when the index found one
    assert "a time span ('last year', '2023–2024') goes in drive_date_from / drive_date_to, not in the words" in text


def test_the_date_filter_says_which_date_it_matches():
    text = _SCHEMA["drive_date_from"]
    # D25 (§25.5.7): photos by when they were taken, documents by the date written in them when the index found one,
    # everything else by the file's date; still no per-kind subtlety (scans' own date) in model-facing text
    assert ("a photo's taken date, otherwise the date written in the document when the index found one, otherwise "
            "the file's date") in text
    assert "own date" not in text and "scans" not in text


def test_capabilities_say_the_drive_answers_from_content():
    assert "I answer from what is written in your documents" in CAPABILITIES_TEXT
    assert "I read scans, screenshots and photographed receipts" in CAPABILITIES_TEXT
    assert "not what is inside the files" not in CAPABILITIES_TEXT
