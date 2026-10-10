"""Drive intents on FILE_MANAGEMENT (docs/10_rfcs/USER_DRIVE_RFC.md §3, §4.3–§4.5, §4.9).

Step 1 has seven drive intents: search is not available inside the App Folder (RFC §7, A6).
"""
import re

from src.infrastructure.agent_manifest import FILE_MANAGEMENT, Intent
from src.infrastructure.agent_registry import ExecutionMode

_DRIVE = {
    Intent.SAVE_FILE_TO_DRIVE: "save_file_to_drive",
    Intent.LIST_FILES_IN_DRIVE: "list_files_in_drive",
    Intent.OPEN_FILE_FROM_DRIVE: "open_file_from_drive",
    Intent.MOVE_FILE_IN_DRIVE: "move_file_in_drive",
    Intent.CREATE_FOLDER_IN_DRIVE: "create_folder_in_drive",
    Intent.UPDATE_FILE_IN_DRIVE: "update_file_in_drive",
    Intent.DELETE_FILE_FROM_DRIVE: "delete_file_from_drive",
}
_PROVIDER = re.compile(r"onedrive|microsoft|google", re.I)


def test_values_and_sync():
    for const, value in _DRIVE.items():
        assert const == value
        assert FILE_MANAGEMENT.capabilities[const] == ExecutionMode.SYNC


def test_no_search_intent_in_step_one():
    assert not hasattr(Intent, "SEARCH_FILES_IN_DRIVE")
    assert "search_files_in_drive" not in FILE_MANAGEMENT.capabilities
    assert "search_files_in_drive" not in FILE_MANAGEMENT.context_schemas
    assert "search_files_in_drive" not in FILE_MANAGEMENT.capability_descriptions
    for schema in FILE_MANAGEMENT.context_schemas.values():
        assert "search_text" not in schema


def test_no_description_offers_drive_search():
    drive_texts = [FILE_MANAGEMENT.capability_descriptions[i] for i in _DRIVE]
    drive_texts.append(FILE_MANAGEMENT.description)
    for schema_intent in _DRIVE:
        drive_texts += [str(v) for v in FILE_MANAGEMENT.context_schemas.get(schema_intent, {}).values()]
    for text in drive_texts:
        low = text.lower()
        assert "search" not in low and "find files" not in low and "look up" not in low, text


def test_no_prefetch():
    assert FILE_MANAGEMENT.prefetch_file_ref is False


def test_schemas():
    s = FILE_MANAGEMENT.context_schemas
    assert {"file_ref", "folder", "name"} <= set(s[Intent.SAVE_FILE_TO_DRIVE])
    assert {"file_ref", "folder", "new_name"} <= set(s[Intent.MOVE_FILE_IN_DRIVE])
    assert set(s[Intent.UPDATE_FILE_IN_DRIVE]) == {"file_ref", "append_text", "source_ref"}


def test_no_provider_name_reaches_the_model():
    texts = [FILE_MANAGEMENT.description]
    texts += [FILE_MANAGEMENT.capability_descriptions[i] for i in FILE_MANAGEMENT.capabilities]
    for schema in FILE_MANAGEMENT.context_schemas.values():
        texts += [str(v) for v in schema.values()]
    for text in texts:
        assert not _PROVIDER.search(text), text


def test_save_says_chat_files_are_temporary():
    text = FILE_MANAGEMENT.capability_descriptions[Intent.SAVE_FILE_TO_DRIVE].lower()
    assert "temporary" in text and "remember" in text


def test_only_file_management_opts_out_of_prefetch():
    """Default True everywhere else: no other agent silently loses its pre-fetched file_content."""
    from src.infrastructure.agent_manifest import ALL_DESCRIPTORS
    opted_out = {d.agent_id for d in ALL_DESCRIPTORS if not d.prefetch_file_ref}
    assert opted_out == {FILE_MANAGEMENT.agent_id}


def test_descriptions_disambiguate_the_routing_cases():
    """RFC §3/§4.4/§4.9 routing cases, pinned to the wording the model reads."""
    d = FILE_MANAGEMENT.capability_descriptions
    # "remember this FILE" vs "remember THAT ...": the file intent points text and facts elsewhere.
    assert "save_to_memory" in d[Intent.SAVE_FILE_TO_DRIVE]
    # A chat file opened earlier, then "move it to <folder>": not on the drive yet, so save, not move.
    assert "save_file_to_drive" in d[Intent.MOVE_FILE_IN_DRIVE]
    # The store is in the name, both directions, for the destructive pair.
    assert "delete_file_from_drive" in d[Intent.DELETE_FILE]
    assert "[File: ...]" in d[Intent.DELETE_FILE] and "[Drive: ...]" in d[Intent.DELETE_FILE]
    # update: two safe modes, no full-text overwrite offered.
    update = d[Intent.UPDATE_FILE_IN_DRIVE]
    assert "append_text" in update and "source_ref" in update
    assert "Rewriting a file with new text is not available" in update
    # Reading from the drive is addressed by [Drive: ...] label and drive:<id> ref.
    assert "drive:<id>" in d[Intent.OPEN_FILE_FROM_DRIVE] and "[Drive: ...]" in d[Intent.OPEN_FILE_FROM_DRIVE]
