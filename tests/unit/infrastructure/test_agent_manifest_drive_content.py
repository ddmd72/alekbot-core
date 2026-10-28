"""search_files_in_drive answers from content (USER_DRIVE_INDEXING_RFC §24.10, D11, D13; §17.7; Search v2 D25,
§25.13.6).

The intent stays as it is; its description and the drive_query field tell Smart that content questions go into
drive_query (a subject, a phrase, a number, an amount, an id), and the date fields say which date a filter matches.
The user-facing capabilities text says the drive answers from content and reads scans."""
from src.domain import drive_tool_texts as tt
from src.domain.drive_tool_texts import drive_tool_declarations
from src.utils.capabilities import CAPABILITIES_TEXT

# Retargeted at the cut (DRIVE_SPECIALIST_RFC §7.6, §8.1): the search fields are the drive specialist's `search_drive`
# parameters now, and the intent's description is that tool's description.
_SCHEMA = {"drive_query": tt.QUERY, "drive_date_from": tt.DATE_FROM, "drive_date_to": tt.DATE_TO}
_DESCRIPTION = next(t["description"] for t in drive_tool_declarations() if t["name"] == tt.SEARCH_DRIVE)


def test_the_description_offers_content_search():
    # Reviewer edit at the cut: the capability is offered to Smart by its one drive intent, whose text the RFC gives
    # verbatim (DRIVE_SPECIALIST_RFC §4.1: "by what a file is or what is written inside (a subject, a phrase, a
    # number, an amount)", "(documents, scans, photos, recordings)"); the search tool's own line is §8.1's
    # ("what is written in them"); a passage match is read through the passage reading line (§7.3).
    from src.domain import drive_answer_texts as t
    from src.infrastructure.agent_manifest import DRIVE_SPECIALIST, Intent
    smart = DRIVE_SPECIALIST.capability_descriptions[Intent.WORK_WITH_DRIVE_FILES]
    for phrase in ("what is written inside (a subject, a phrase, a number, an amount)", "scans"):
        assert phrase in smart, phrase
    assert "what is written in them" in _DESCRIPTION
    # Reviewer (USER_DRIVE_INDEXING_RFC §28.3 A13): the card's window is named "a search window"
    assert t.R_PASSAGE.startswith("A card's passage is a search window cut from the file around what matched.")


def test_content_words_go_into_drive_query():
    text = _SCHEMA["drive_query"]
    assert "or what to find inside (a phrase, a number, an amount, an id)" in text
    for example in ("'tickets'", "'receipts'", "'contracts'", "'what expires'"):   # D25: document types allowed
        assert example in text, example
    # D25 replaces review E-M3's rule: a time span goes in the date fields, which match the date written in the
    # document when the index found one
    assert "a time span ('last year', '2023–2024') goes in date_from / date_to, not in the words" in text


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
