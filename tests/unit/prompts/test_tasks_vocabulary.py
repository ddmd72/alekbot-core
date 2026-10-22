"""H4 vocabulary table for Tasks (TEST_COVERAGE_SYSTEMIC_RFC §3 H4, P8).

Production renders `prompts_snapshot/profiles/tasks.yaml` (blueprint `tasks_agent_v1`). The Tasks tokens are prose that
names tools, arguments and result fields; the code owns `_TOOL_DECLARATIONS` and the shape `_format_task_list` returns.
Out of scope: USER/ACCOUNT overrides (not mirrored)."""
import re
from datetime import datetime

import pytest

from src.agents.tasks_agent import _TOOL_DECLARATIONS, TasksAgent
from src.domain.task import (ChecklistItem, LinkedResource, RecurrencePattern, RecurrenceRange, Task, TaskAttachment,
                             TaskRecurrence, TaskStatus)
from tests.support import prompt_vocabulary as pv

PROFILE = pv.load_profile("tasks")
BLUEPRINT = pv.load_blueprint(PROFILE["blueprint_id"])
TOKENS = pv.load_tokens(PROFILE)
PROSE = {t: body for t, (_, body) in TOKENS.items()}

CODE_TOOLS = {t["name"]: t["parameters"] for t in _TOOL_DECLARATIONS}

# Tools the tokens may name: a name matching this pattern must be a declared tool.
TOOL_NAME = r"\b[a-z]+_tasks?\b"

# Result fields the tokens tell the model to read or return (token -> fields); each is a key of a serialized task.
RESULT_FIELDS = {
    "COGNITIVE_PROCESS_TASKS": {"ref"},   # search/list results carry the short id as `ref`; update/delete take it as `task_ref`
    "OUTPUT_FORMAT_TASKS": {"ref", "title", "status", "importance", "tags", "due_datetime", "reminder_datetime", "body",
                            "checklist_items", "linked_resources", "recurrence", "attachments"},   # the list the token states
}

# Enum row: the status values the prose lists == the update_task schema's enum == TaskStatus.
STATUS_ROW = ("COGNITIVE_PROCESS_TASKS", "Accepted values:")


def _all_keys(node, acc=None):
    """Every property name in a tool schema, nested ones included."""
    acc = set() if acc is None else acc
    for name, sub in node.get("properties", {}).items():
        acc.add(name)
        _all_keys(sub, acc)
    return acc


def _result_task():
    now = datetime(2026, 1, 1)
    task = Task(
        task_id="t1", list_id="l1", list_name="Tasks", user_id="u", title="x", body="b", due_datetime=now,
        reminder_datetime=now, completed_at=now, tags=["a"],
        recurrence=TaskRecurrence(RecurrencePattern(type="daily"), RecurrenceRange(type="noEnd", start_date="2026-01-01")),
        checklist_items=[ChecklistItem(item_id="c", title="c", checked_at=now)],
        attachments=[TaskAttachment(attachment_id="a", filename="f", url="u", gcs_uri="g")],
        linked_resources=[LinkedResource(resource_id="r", web_url="w", display_name="d", application_name="n")],
    )
    return object.__new__(TasksAgent)._format_task_list([task])["tasks"][0]   # no state of its own is read


def _keys(result):
    def walk(v):
        if isinstance(v, dict):
            return set(v) | {k for x in v.values() for k in walk(x)}
        if isinstance(v, list):
            return {k for x in v for k in walk(x)}
        return set()
    return walk(result)


RESULT_TASK = _result_task()
RESULT_KEYS = _keys(RESULT_TASK)


def _words(text):
    return set(re.findall(r"[A-Za-z_]+", text))


def test_every_profile_token_exists_and_the_blueprint_orders_each_class_once():
    classes = [cls for cls, _ in TOKENS.values()]
    assert len(classes) == len(set(classes)), f"two profile tokens share a class: {classes}"
    assert sorted(classes) == sorted(BLUEPRINT["class_order"]), (
        f"blueprint orders {BLUEPRINT['class_order']}, profile tokens have {classes}")


def test_every_tool_the_tokens_name_is_declared_and_every_declared_tool_is_named():
    named = {m for body in PROSE.values() for m in re.findall(TOOL_NAME, body)}
    assert named - set(CODE_TOOLS) == set(), f"tokens name tools the code does not declare: {named - set(CODE_TOOLS)}"
    assert set(CODE_TOOLS) - named == set(), f"declared tools no token names: {set(CODE_TOOLS) - named}"


def test_every_required_argument_is_named_by_a_token():
    prose_words = set().union(*(_words(b) for b in PROSE.values()))
    missing = {(tool, arg) for tool, spec in CODE_TOOLS.items() for arg in spec.get("required", []) if arg not in prose_words}
    assert not missing, f"required arguments no Tasks token names: {sorted(missing)}"


def test_identifier_fields_the_tokens_name_exist_in_the_schema_or_the_result():
    known = set().union(*(_all_keys(spec) for spec in CODE_TOOLS.values())) | RESULT_KEYS
    named = {w for body in PROSE.values() for w in _words(body) if re.fullmatch(r"[a-z]+(?:_[a-z]+)*_(?:id|ref)", w)}
    assert named <= known, f"tokens name id fields that are neither an argument nor a result field: {sorted(named - known)}"


@pytest.mark.parametrize("token,fields", sorted(RESULT_FIELDS.items()), ids=sorted(RESULT_FIELDS))
def test_result_fields_a_token_names_are_what_a_search_returns(token, fields):
    assert fields <= RESULT_KEYS, f"{fields - RESULT_KEYS} is not a key of a serialized task ({sorted(RESULT_KEYS)})"
    assert fields <= _words(PROSE[token]), f"{token} does not name {sorted(fields - _words(PROSE[token]))}"


def test_sub_fields_the_prose_names_in_parentheses_are_keys_of_that_result_field():
    """`linked_resources (display_name + web_url)`: each word must be a key of the entries of that field."""
    checked = 0
    for token, body in PROSE.items():
        for name, words in re.findall(r"\b([a-z_]+) \(([a-z_]+(?: \+ [a-z_]+)*)\)", body):
            value = RESULT_TASK.get(name)
            entry = value[0] if isinstance(value, list) else value
            if isinstance(entry, dict):
                checked += 1
                missing = set(words.split(" + ")) - set(entry)
                assert not missing, f"{token}: {name} ({words}) names {sorted(missing)}; the result has {sorted(entry)}"
    assert checked >= 3   # linked_resources, recurrence, attachments


def test_every_tool_call_the_prose_writes_uses_a_declared_argument_and_value():
    """`update_task(status=completed)`: the tool is declared, the argument is one of its properties, an enum holds the value."""
    calls = [(tool, arg, val) for body in PROSE.values() for tool, arg, val in re.findall(r"\b(\w+)\((\w+)=(\w+)\)", body)]
    assert calls, "the tokens no longer show a call with an argument"
    for tool, arg, val in calls:
        assert tool in CODE_TOOLS, f"{tool}({arg}={val}): no such tool"
        props = CODE_TOOLS[tool].get("properties", {})
        assert arg in props, f"{tool}({arg}={val}): {arg} is not an argument ({sorted(props)})"
        assert "enum" not in props[arg] or val in props[arg]["enum"], f"{tool}({arg}={val}): {val} is not in {props[arg]['enum']}"


def test_status_values_the_prose_lists_are_the_schema_enum():
    token, marker = STATUS_ROW
    line = next(l for l in PROSE[token].split("\n") if marker in l)
    listed = set(re.findall(r"\w+", line.split(marker, 1)[1]))
    schema = set(CODE_TOOLS["update_task"]["properties"]["status"]["enum"])
    assert listed == schema == {s.value for s in TaskStatus}


def test_edited_tokens_round_trip_through_the_serializer():
    for token_id in TOKENS:
        text = pv.token_path(token_id).read_text(encoding="utf-8")
        assert pv.ser.token_to_file(pv.ser.token_from_file(text)) == text
