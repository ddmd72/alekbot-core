"""The field enumeration P14 rests on (card A): the flattening rules, the surfaces it reads, and `unused`."""
from tests.support.tool_fields import (
    IN_SCOPE, READ_ONLY, READ_ONLY_FIELDS, SURFACES, declared, flatten, tool_of, unused, used,
)

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "recurrence": {"type": "object", "properties": {
            "pattern": {"type": "string"},
            "range": {"type": "object", "properties": {"end": {"type": "string"}}},
        }},
        "files": {"type": "array", "items": {"type": "object", "properties": {
            "path": {"type": "string"}, "remove": {"type": "boolean"}}}},
        "tags": {"type": "array", "items": {"type": "string"}},
        "metadata": {"type": "object", "description": "free-form"},
    },
}


def test_flatten_recurses_objects_lifts_arrays_of_objects_and_keeps_leaves():
    assert sorted(flatten(SCHEMA)) == [
        "files[].path", "files[].remove",      # an array of objects has no path of its own
        "metadata",                            # an object without properties is a leaf
        "recurrence.pattern", "recurrence.range.end",
        "tags",                                # an array of scalars is a leaf
        "title",
    ]


def test_used_takes_every_path_with_prefixes_and_descends_only_into_lists_of_dicts():
    assert used({"fact_id": "f", "updates": {"metadata": {"k": 1}, "tags": ["a"]},
                 "files": [{"path": "p"}, {"remove": True}]}) == {
        "fact_id", "updates", "updates.metadata", "updates.metadata.k", "updates.tags",
        "files", "files[].path", "files[].remove",
    }


def test_declared_contains_the_fields_of_every_surface():
    keys = set(declared())
    for key in (
        "consolidation:update_fact.updates.metadata",
        "consolidation_review:update_fact.payment.paid",
        "notes:create_self_reminder.complexity",
        "tasks:update_task.recurrence.days_of_week",
        "smart_local:draft_skill.files[].from_file",
        "delegate:save_file_to_drive.text",
        "delegate:manage_self_reminders.workspace_file",
        "delegate:open_file_from_drive.original",
    ):
        assert key in keys, key
    assert "delegate:search_files_in_drive.drive_query" not in keys  # not P14's: search writes nothing
    assert "smart_local:draft_skill.files[]" not in keys            # an array of objects has no path of its own
    assert IN_SCOPE == ("consolidation", "consolidation_review", "notes", "tasks", "smart_local", "delegate")


def test_every_read_only_tool_and_field_is_declared():
    keys = declared()
    tools = {tool_of(k) for k in keys}
    assert READ_ONLY <= tools, sorted(READ_ONLY - tools)
    assert READ_ONLY_FIELDS <= set(keys), sorted(READ_ONLY_FIELDS - set(keys))


def test_the_delegate_surface_is_the_context_schema_smart_declares():
    by_name = {d["name"]: d for d in SURFACES["delegate"]()}
    assert sorted(by_name) == sorted([
        "save_file_to_drive", "list_files_in_drive", "open_file_from_drive", "move_file_in_drive",
        "create_folder_in_drive", "update_file_in_drive", "delete_file_from_drive",
        "manage_self_reminders", "manage_user_tasks",
    ])
    assert by_name["open_file_from_drive"]["parameters"]["properties"]["original"]["type"] == "boolean"
    assert by_name["manage_user_tasks"]["parameters"]["properties"]["background"]["type"] == "string"


def test_unused_lists_what_no_call_sends():
    assert unused("notes", "create_self_reminder", [{"text": "x", "due": "y"}]) == [
        "complexity", "instruction", "recurrence"]

    update_left = unused("consolidation", "update_fact",
                         [{"fact_id": "f", "updates": {"metadata": {"k": 1}, "tags": ["a"]}}])
    assert "updates.metadata" not in update_left and "updates.tags" not in update_left
    assert "updates.content" in update_left

    draft_left = unused("smart_local", "draft_skill", [{"files": [{"path": "p", "remove": True}]}])
    assert "files[].path" not in draft_left and "files[].remove" not in draft_left
    assert "files[].content" in draft_left and "name" in draft_left


def test_unused_takes_the_union_of_several_calls():
    calls = [{"note_id": "1", "recurrence": "FREQ=DAILY"}, {"note_id": "1", "clear_recurrence": True}]
    assert unused("notes", "update_self_reminder", calls) == ["complexity", "due", "instruction", "text"]
