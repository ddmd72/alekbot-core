"""`drive_deleted` on search_files_in_drive (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.9.11, D48; the same flag as
docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md §6.4). The delegate tool keeps the first description seen per field name, so the
two features must share one description and no other intent may use the name."""
from src.agents.base_agent import BaseAgent
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS, FILE_MANAGEMENT, Intent
from src.infrastructure.agent_registry import AgentRegistry

# Every search_files_in_drive field description names its intent (test_agent_manifest_drive_search.py), the shared one too.
SHARED = ("search_files_in_drive: true only when the user explicitly asks about deleted or removed files: searches "
          "transcripts of deleted recordings and your notes on deleted files. Never for an ordinary search.")


def test_field_in_schema_and_description():
    schema = FILE_MANAGEMENT.context_schemas[Intent.SEARCH_FILES_IN_DRIVE]
    assert schema["drive_deleted"] == SHARED
    prose = FILE_MANAGEMENT.capability_descriptions[Intent.SEARCH_FILES_IN_DRIVE]
    assert '"drive_deleted"' in prose            # named in the Context block (the prose/schema test reads it)


def test_the_delegate_tool_carries_the_field_with_the_shared_description():
    registry = AgentRegistry()
    registry.register(FILE_MANAGEMENT)
    declaration = BaseAgent._build_delegate_tool_declaration(registry._describe(include_internal=True))
    props = declaration["parameters"]["properties"]["context"]["properties"]
    assert props["drive_deleted"]["description"] == SHARED


def test_name_unique_across_intents():
    owners = [(d.agent_id, intent) for d in ALL_DESCRIPTORS for intent, schema in d.context_schemas.items()
              if "drive_deleted" in schema]
    assert owners == [(FILE_MANAGEMENT.agent_id, Intent.SEARCH_FILES_IN_DRIVE)]


def test_description_says_explicitly():
    assert "explicitly" in SHARED and "Never for an ordinary search" in SHARED
    prose = FILE_MANAGEMENT.capability_descriptions[Intent.SEARCH_FILES_IN_DRIVE]
    assert "only when the user explicitly asks about deleted or removed files" in prose
    assert "Never for an ordinary search" in prose
