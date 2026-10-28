"""Drive intents on FILE_MANAGEMENT (docs/10_rfcs/USER_DRIVE_RFC.md §3, §4.3–§4.5, §4.9).

The seven Step 1 drive intents. They do not offer search themselves: the App Folder has no provider search
(RFC §7, A6). Search is `search_files_in_drive` over the drive index (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md),
tested in test_agent_manifest_drive_search.py.
"""
import re

from src.infrastructure.agent_manifest import FILE_MANAGEMENT, Intent

_PROVIDER = re.compile(r"onedrive|microsoft|google", re.I)


def test_no_search_text_field():
    for schema in FILE_MANAGEMENT.context_schemas.values():
        assert "search_text" not in schema


def test_no_prefetch():
    assert FILE_MANAGEMENT.prefetch_file_ref is False


def test_no_provider_name_reaches_the_model():
    texts = [FILE_MANAGEMENT.description]
    texts += [FILE_MANAGEMENT.capability_descriptions[i] for i in FILE_MANAGEMENT.capabilities]
    for schema in FILE_MANAGEMENT.context_schemas.values():
        texts += [str(v) for v in schema.values()]
    for text in texts:
        assert not _PROVIDER.search(text), text


def test_save_says_chat_files_are_temporary():
    from src.infrastructure.agent_manifest import DRIVE_SPECIALIST
    text = DRIVE_SPECIALIST.capability_descriptions[Intent.WORK_WITH_DRIVE_FILES].lower()   # §4.1
    assert "temporary" in text and "remember" in text


def test_only_file_management_opts_out_of_prefetch():
    """Default True everywhere else: no other agent silently loses its pre-fetched file_content."""
    from src.infrastructure.agent_manifest import ALL_DESCRIPTORS, DRIVE_SPECIALIST
    opted_out = {d.agent_id for d in ALL_DESCRIPTORS if not d.prefetch_file_ref}
    assert opted_out == {FILE_MANAGEMENT.agent_id, DRIVE_SPECIALIST.agent_id}


def test_descriptions_disambiguate_the_routing_cases():
    """RFC §3/§4.4/§4.9 routing cases, pinned to the wording the model reads. Reviewer edit at the cut
    (DRIVE_SPECIALIST_RFC §4.1, §4.4, §5.2, §8.1): Smart reads its one drive intent and DELETE_FILE; the specialist
    reads its tools and its PROTOCOL_DRIVE_SEARCH token, which carries the Step 1 rules of the 8 descriptions.
    Reviewer (owner decision 2026-10-28): the two token assertions went (no specialist-token wording pins); the tool
    declarations and Smart's descriptions stay pinned."""
    from src.domain import drive_tool_texts as tt
    from src.infrastructure.agent_manifest import DRIVE_SPECIALIST
    smart = DRIVE_SPECIALIST.capability_descriptions[Intent.WORK_WITH_DRIVE_FILES]
    tools = {t["name"] for t in tt.drive_tool_declarations()}
    d = FILE_MANAGEMENT.capability_descriptions
    # "remember this FILE" vs "remember THAT ...": the file intent points facts elsewhere.
    assert "propose_memory_facts" in smart
    # The store is in the name, both directions, for the destructive pair.
    assert "work_with_drive_files" in d[Intent.DELETE_FILE]
    assert "[File: ...]" in d[Intent.DELETE_FILE] and "[Drive: ...]" in d[Intent.DELETE_FILE]
    # update: two safe modes (one tool each), no full-text overwrite offered.
    assert {tt.APPEND_TO_FILE, tt.REPLACE_FILE_CONTENT} <= tools
    assert "rewriting with new text is not available" in smart
    # Reading from the drive is addressed by [Drive: …] label and drive: ref.
    assert "drive: ref" in tt.OPEN_REF and "[Drive: …] label" in tt.OPEN_REF
