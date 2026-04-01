"""
Unit tests for agent_manifest.py — file storage intents and descriptors.

Covers:
  Intent constants (OPEN_FILE, DELETE_FILE)
  FILE_MANAGEMENT descriptor (capabilities, context_schemas)
  Doc generator descriptors (file_ref in context_schemas)
  ALL_DESCRIPTORS registration
"""

from src.infrastructure.agent_manifest import (
    Intent,
    ALL_DESCRIPTORS,
    FILE_MANAGEMENT,
    DOC_PLANNER,
    PDF_GENERATOR,
    HTML_PAGE_GENERATOR,
)
from src.infrastructure.agent_registry import ExecutionMode


class TestFileStorageIntents:

    def test_open_file_value(self):
        assert Intent.OPEN_FILE == "open_file"

    def test_delete_file_value(self):
        assert Intent.DELETE_FILE == "delete_file"


class TestFileManagementDescriptor:

    def test_registered_in_all_descriptors(self):
        assert FILE_MANAGEMENT in ALL_DESCRIPTORS

    def test_agent_id(self):
        assert FILE_MANAGEMENT.agent_id == "file_management_agent"

    def test_agent_type(self):
        assert FILE_MANAGEMENT.agent_type == "file_management"

    def test_capabilities_both_sync(self):
        assert FILE_MANAGEMENT.capabilities[Intent.OPEN_FILE] == ExecutionMode.SYNC
        assert FILE_MANAGEMENT.capabilities[Intent.DELETE_FILE] == ExecutionMode.SYNC

    def test_context_schemas_file_ref(self):
        fetch_schema = FILE_MANAGEMENT.context_schemas[Intent.OPEN_FILE]
        delete_schema = FILE_MANAGEMENT.context_schemas[Intent.DELETE_FILE]
        assert "file_ref" in fetch_schema
        assert "file_ref" in delete_schema

    def test_not_internal(self):
        assert FILE_MANAGEMENT.internal is False

    def test_has_capability_descriptions(self):
        assert Intent.OPEN_FILE in FILE_MANAGEMENT.capability_descriptions
        assert Intent.DELETE_FILE in FILE_MANAGEMENT.capability_descriptions


class TestDocGeneratorsFileRefSchema:

    def test_doc_planner_has_file_ref(self):
        schema = DOC_PLANNER.context_schemas.get(Intent.CREATE_DOCUMENT, {})
        assert "file_ref" in schema

    def test_pdf_generator_has_file_ref(self):
        schema = PDF_GENERATOR.context_schemas.get(Intent.CREATE_PDF, {})
        assert "file_ref" in schema

    def test_html_page_generator_has_file_ref(self):
        schema = HTML_PAGE_GENERATOR.context_schemas.get(Intent.CREATE_HTML_PAGE, {})
        assert "file_ref" in schema
