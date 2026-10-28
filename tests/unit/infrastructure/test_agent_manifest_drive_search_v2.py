"""search_files_in_drive's Search v2 contract text (USER_DRIVE_INDEXING_RFC §25.5.7, verbatim §25.13.6, D25): the
manifest field descriptions are the frozen `drive_search_texts` strings, and the capability description names the
document-type queries and the tiers without adding a field (D19: no new Smart field, no mode)."""
from src.domain import drive_tool_texts as tt
from src.domain.drive_tool_texts import drive_tool_declarations
from src.infrastructure.agent_manifest import Intent

# Retargeted at the cut (DRIVE_SPECIALIST_RFC §7.6, §8.1): the search fields are `search_drive`'s parameters, their
# texts the specialist-owned constants of drive_tool_texts.py.
_SCHEMA = {name: spec["description"] for name, spec in next(
    t for t in drive_tool_declarations() if t["name"] == tt.SEARCH_DRIVE)["parameters"]["properties"].items()}


def test_the_field_texts_are_the_frozen_ones():
    assert _SCHEMA["query"] == tt.QUERY
    assert _SCHEMA["kind"] == tt.KIND
    assert _SCHEMA["date_from"] == tt.DATE_FROM
    assert _SCHEMA["date_to"] == tt.DATE_TO


def test_time_spans_go_to_the_date_fields_and_types_to_the_query():
    assert "goes in date_from / date_to, not in the words" in _SCHEMA["query"]
    assert "'tickets', 'receipts', 'contracts', 'what expires'" in _SCHEMA["query"]
    assert "document (docx, doc, odt, rtf, pdf, markdown, text)" in _SCHEMA["kind"]


def test_no_new_field_and_no_mode():
    # D19 held for Search v2: no v2 field, no mode. The deleted-files search is its own tool now
    # (search_deleted_recordings_and_notes, DRIVE_SPECIALIST_RFC §8.1), not a field.
    assert set(_SCHEMA) == {"query", "folder", "kind", "date_from", "date_to"}


def test_the_capability_names_document_types_and_the_tiers():
    # Reviewer edit at the cut: Smart's one drive intent offers the document-type search in the RFC's words
    # (DRIVE_SPECIALIST_RFC §4.1, "by what a file is"), the searcher's `query` names the types (D25), and the tiers
    # reach both halves: the specialist's token and Smart's tiers reading line (§5.7, §7.3). Reviewer (owner decision
    # 2026-10-28): only Smart's half is pinned now; the token's wording is tuned on evals.
    from src.domain import drive_answer_texts as t
    from src.infrastructure.agent_manifest import DRIVE_SPECIALIST
    assert "by what a file is" in DRIVE_SPECIALIST.capability_descriptions[Intent.WORK_WITH_DRIVE_FILES]
    assert "'tickets', 'receipts', 'contracts'" in _SCHEMA["query"]
    assert "'Related by meaning only' lines are guesses" in t.R_TIERS
