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
    assert "a date the user quotes from inside a document may stay" in text


def test_the_date_filter_says_which_date_it_matches():
    text = _SCHEMA["drive_date_from"]
    assert "when a photo was taken" in text and "the document's own date for scans" in text
    assert "otherwise when the file was last changed" in text


def test_capabilities_say_the_drive_answers_from_content():
    assert "I answer from what is written in your documents" in CAPABILITIES_TEXT
    assert "I read scans, screenshots and photographed receipts" in CAPABILITIES_TEXT
    assert "not what is inside the files" not in CAPABILITIES_TEXT
