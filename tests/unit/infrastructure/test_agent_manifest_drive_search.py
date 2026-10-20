"""search_files_in_drive on FILE_MANAGEMENT (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §10.4, §17.7).

The delegate tool keeps the FIRST description seen per context field name
(BaseAgent._build_delegate_tool_declaration), so a field shared with another intent under another
description would silently lose ours. The new fields are named so no other intent uses them, and this
file fails the moment one does.
"""
from src.agents.base_agent import BaseAgent
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS, FILE_MANAGEMENT, Intent
from src.infrastructure.agent_registry import AgentRegistry, ExecutionMode

SEARCH_FIELDS = ("drive_query", "drive_folder", "drive_kind", "drive_date_from", "drive_date_to")


def test_intent_value_and_mode():
    assert Intent.SEARCH_FILES_IN_DRIVE == "search_files_in_drive"
    assert FILE_MANAGEMENT.capabilities[Intent.SEARCH_FILES_IN_DRIVE] == ExecutionMode.SYNC
    assert FILE_MANAGEMENT.internal is False


def test_schema_has_exactly_the_unique_fields():
    assert tuple(FILE_MANAGEMENT.context_schemas[Intent.SEARCH_FILES_IN_DRIVE]) == SEARCH_FIELDS


def _clashes(descriptors):
    ours = FILE_MANAGEMENT.context_schemas[Intent.SEARCH_FILES_IN_DRIVE]
    return [
        (d.agent_id, intent, name)
        for d in descriptors
        for intent, schema in d.context_schemas.items()
        for name, spec in schema.items()
        if name in ours and not (d is FILE_MANAGEMENT and intent == Intent.SEARCH_FILES_IN_DRIVE)
        and spec != ours[name]
    ]


def test_no_other_intent_uses_a_search_field_with_another_description():
    assert _clashes(ALL_DESCRIPTORS) == []


def test_the_clash_check_sees_a_clash():
    """Guard the check itself, so an empty list cannot pass vacuously."""
    from dataclasses import replace
    intruder = replace(FILE_MANAGEMENT, agent_id="intruder",
                       context_schemas={"other_intent": {"drive_query": "something else"}})
    assert _clashes([*ALL_DESCRIPTORS, intruder]) == [("intruder", "other_intent", "drive_query")]


def test_the_model_receives_our_descriptions():
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    declaration = BaseAgent._build_delegate_tool_declaration(registry.get_available_intents())
    props = declaration["parameters"]["properties"]["context"]["properties"]
    ours = FILE_MANAGEMENT.context_schemas[Intent.SEARCH_FILES_IN_DRIVE]
    for name in SEARCH_FIELDS:
        assert props[name] == {"type": "string", "description": ours[name]}


def test_filter_only_guidance_is_where_the_model_reads_it():
    query = FILE_MANAGEMENT.context_schemas[Intent.SEARCH_FILES_IN_DRIVE]["drive_query"]
    assert "EMPTY" in query and "filter-only" in query
    text = FILE_MANAGEMENT.capability_descriptions[Intent.SEARCH_FILES_IN_DRIVE]
    assert "leave drive_query empty" in text and "open_file_from_drive" in text
    assert "before saying a file is not there" in text


def test_every_field_is_described_as_belonging_to_the_intent():
    for name, spec in FILE_MANAGEMENT.context_schemas[Intent.SEARCH_FILES_IN_DRIVE].items():
        assert spec.startswith("search_files_in_drive: "), name


def test_kind_description_names_the_families():
    text = FILE_MANAGEMENT.context_schemas[Intent.SEARCH_FILES_IN_DRIVE]["drive_kind"]
    for family in ("spreadsheet", "document", "presentation", "photo"):
        assert family in text


# Names that already collide with differing descriptions (§10.4). Each is a silent
# "first description wins" today; fix one, then remove it here. A new name never joins this list.
KNOWN_COLLISIONS = frozenset({
    "file_ref",     # TODO: nine intents describe it differently
    "folder",       # TODO: four drive intents
    "resolution",   # TODO: image and video generation
})


def _colliding_names(descriptors):
    seen = {}
    for d in descriptors:
        for schema in d.context_schemas.values():
            for name, spec in schema.items():
                seen.setdefault(name, set()).add(repr(spec))
    return {name for name, specs in seen.items() if len(specs) > 1}


def test_no_new_field_name_collides_anywhere():
    """§10.4 / §17.7: a shared field name with differing descriptions fails, except the known three."""
    assert _colliding_names(ALL_DESCRIPTORS) - KNOWN_COLLISIONS == set()


def test_the_allowlist_only_holds_live_collisions():
    """A fixed collision must leave the list, so it cannot quietly come back."""
    assert KNOWN_COLLISIONS <= _colliding_names(ALL_DESCRIPTORS)
