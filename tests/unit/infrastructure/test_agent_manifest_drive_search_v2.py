"""search_files_in_drive's Search v2 contract text (USER_DRIVE_INDEXING_RFC §25.5.7, verbatim §25.13.6, D25): the
manifest field descriptions are the frozen `drive_search_texts` strings, and the capability description names the
document-type queries and the tiers without adding a field (D19: no new Smart field, no mode)."""
from src.domain.drive_search_texts import (
    DRIVE_DATE_FROM_DESCRIPTION, DRIVE_DATE_TO_DESCRIPTION, DRIVE_KIND_DESCRIPTION, DRIVE_QUERY_DESCRIPTION,
)
from src.infrastructure.agent_manifest import FILE_MANAGEMENT, Intent

_SCHEMA = FILE_MANAGEMENT.context_schemas[Intent.SEARCH_FILES_IN_DRIVE]


def test_the_field_texts_are_the_frozen_ones():
    assert _SCHEMA["drive_query"] == DRIVE_QUERY_DESCRIPTION
    assert _SCHEMA["drive_kind"] == DRIVE_KIND_DESCRIPTION
    assert _SCHEMA["drive_date_from"] == DRIVE_DATE_FROM_DESCRIPTION
    assert _SCHEMA["drive_date_to"] == DRIVE_DATE_TO_DESCRIPTION


def test_time_spans_go_to_the_date_fields_and_types_to_the_query():
    assert "goes in drive_date_from / drive_date_to, not in the words" in _SCHEMA["drive_query"]
    assert "'tickets', 'receipts', 'contracts', 'what expires'" in _SCHEMA["drive_query"]
    assert "document (docx, doc, odt, rtf, pdf, markdown, text)" in _SCHEMA["drive_kind"]


def test_no_new_field_and_no_mode():
    assert set(_SCHEMA) == {"drive_query", "drive_folder", "drive_kind", "drive_date_from", "drive_date_to"}


def test_the_capability_names_document_types_and_the_tiers():
    text = FILE_MANAGEMENT.capability_descriptions[Intent.SEARCH_FILES_IN_DRIVE]
    assert "a ticket, a receipt, a contract" in text and "related by meaning" in text
