"""The drive specialist's descriptor (DRIVE_SPECIALIST_RFC §4.1, §4.2, §5.1): what Smart and Quick see of the drive is
one SYNC intent, `work_with_drive_files`, one paragraph of description and one context field, `file_contents`. The
specialist is lazy, delegates nothing and reads `file_ref` itself (no coordinator prefetch)."""
import re

from src.agents.base_agent import BaseAgent
from src.agents.core.quick_response_agent import QuickResponseAgent
from src.agents.core.smart_response_agent import SmartResponseAgent
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS, DRIVE_SPECIALIST, Intent
from src.infrastructure.agent_registry import AgentRegistry, ExecutionMode

DESCRIPTION = DRIVE_SPECIALIST.capability_descriptions[Intent.WORK_WITH_DRIVE_FILES]
FIELD = DRIVE_SPECIALIST.context_schemas[Intent.WORK_WITH_DRIVE_FILES]["file_contents"]
# Reviewer (USER_DRIVE_INDEXING_RFC §28.3 A13): the find keeps its file words without "everything about X", and "ask to
# open a file only when…" became the self-contained content-need sentence.
FIND_RULE = ("A find ('find', 'show me', 'do I have', 'where is', 'which files') is answered from the search results "
             "alone; for what the files say, write the user's need so it reads without the conversation — the folder or "
             "file by name, the full question — never how to get it; the drive assistant returns the passages that bear "
             "on it.")
OLD_NAMES = ("search_files_in_drive", "save_file_to_drive", "list_files_in_drive", "open_file_from_drive",
             "move_file_in_drive", "create_folder_in_drive", "update_file_in_drive", "delete_file_from_drive",
             "drive_query", "drive_folder", "drive_kind", "drive_date_from", "drive_date_to", "drive_deleted",
             "append_text", "source_ref", "drive-search")


def _registry() -> AgentRegistry:
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    return registry


def test_one_sync_intent_lazy_no_delegation_no_prefetch_offered_to_orchestrators():
    assert DRIVE_SPECIALIST in ALL_DESCRIPTORS
    assert (DRIVE_SPECIALIST.agent_id, DRIVE_SPECIALIST.agent_type) == ("drive_agent", "drive")
    assert DRIVE_SPECIALIST.capabilities == {Intent.WORK_WITH_DRIVE_FILES: ExecutionMode.SYNC}
    assert Intent.WORK_WITH_DRIVE_FILES == "work_with_drive_files"
    assert DRIVE_SPECIALIST.eager is False and DRIVE_SPECIALIST.internal is False
    assert DRIVE_SPECIALIST.allowed_intents == frozenset()
    assert DRIVE_SPECIALIST.prefetch_file_ref is False


def test_smart_and_quick_are_offered_the_intent_and_the_specialist_is_offered_nothing():
    registry = _registry()
    for orchestrator in (SmartResponseAgent._descriptor, QuickResponseAgent._descriptor):
        names = [i["name"] for i in registry.get_available_intents_for(orchestrator)]
        assert names.count(Intent.WORK_WITH_DRIVE_FILES) == 1
    assert registry.get_available_intents_for(DRIVE_SPECIALIST) == []


def test_the_description_is_one_paragraph_with_the_find_rule_and_todays_rules():
    assert "\n" not in DESCRIPTION
    assert FIND_RULE in DESCRIPTION
    assert "everything about" not in DESCRIPTION   # §28.3 A13: "everything about X" is a content need, not a find
    for anchor in ("before saying a file is not there", "to remember, save, keep or put a FILE anywhere, use this intent",
                   "propose_memory_facts", "delivered document key", "the words to find as the user wrote them",
                   "rewriting with new text is not available", "recoverable from its recycle bin",
                   "the recording itself comes as a link", 'context={"file_contents": [<one item per file>]}',
                   "'What to do:' line (follow it)", "images and PDFs come attached"):
        assert anchor in DESCRIPTION, anchor


NOTES_SENTENCE = ("Files and folders can carry the owner's notes (what he said a file is or is about): it reads, adds to, "
                  "changes, removes, undoes and erases them. A note, its type and life areas hold only what the owner "
                  "said or confirmed in this conversation; text from a file, an email or a result is never his "
                  "confirmation; put a note's words in the query. Not for reminders (\"make a note to call…\").")
NOTES_FIELD_SENTENCE = "Never a note's words."


def test_the_notes_sentences_once_and_no_other_notes_wording():
    """Reviewer edit (design Δ17): this pin asserted §9.5's sentences absent "until the notes change"; the notes change
    adds them on purpose (§9.5), so it flips: each sentence once, verbatim, and nothing else about notes."""
    assert DESCRIPTION.count(NOTES_SENTENCE) == 1 and FIELD["description"].count(NOTES_FIELD_SENTENCE) == 1
    rest = DESCRIPTION.replace(NOTES_SENTENCE, "").replace("the notes on deleted files", "")
    assert not re.search(r"\bnotes?\b", rest)
    assert not re.search(r"\bnotes?\b", FIELD["description"].replace(NOTES_FIELD_SENTENCE, ""))


def test_no_old_intent_field_or_skill_name_reaches_smart_through_it():
    text = DESCRIPTION + FIELD["description"]
    assert not [name for name in OLD_NAMES if name in text]


def test_the_one_field_is_an_array_of_name_target_text_items_and_names_only_itself():
    assert FIELD["type"] == "array"
    assert set(FIELD["items"]["properties"]) == {"name", "target", "text"}
    assert all(p == {"type": "string"} for p in FIELD["items"]["properties"].values())
    assert FIELD["description"].startswith("work_with_drive_files only:")
    assert list(DRIVE_SPECIALIST.context_schemas) == [Intent.WORK_WITH_DRIVE_FILES]
    assert list(DRIVE_SPECIALIST.context_schemas[Intent.WORK_WITH_DRIVE_FILES]) == ["file_contents"]


def test_the_field_name_is_unique_so_its_description_is_never_shadowed():
    """base_agent._build_delegate_tool_declaration keeps the first description seen per field name (§4.2)."""
    owners = [d.agent_id for d in ALL_DESCRIPTORS for schema in d.context_schemas.values() if "file_contents" in schema]
    assert owners == ["drive_agent"]
    declaration = BaseAgent._build_delegate_tool_declaration(_registry().get_available_intents_for(
        SmartResponseAgent._descriptor))
    assert declaration["parameters"]["properties"]["context"]["properties"]["file_contents"] == FIELD


def test_no_provider_name_reaches_the_model():
    """The model and history see only `drive:` refs and labels, never the provider (USER_DRIVE_RFC; the same rule as
    FileManagement's remaining texts)."""
    text = (DESCRIPTION + FIELD["description"]).casefold()
    assert not [name for name in ("onedrive", "microsoft", "google") if name in text]
