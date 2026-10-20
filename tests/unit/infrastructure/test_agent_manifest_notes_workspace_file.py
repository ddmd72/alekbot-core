"""manage_self_reminders declares workspace_file and its prose shows how to pass it (AGENT_WORKSPACE_RFC §8.3).
The prose-vs-schema rule itself is test_manifest_prose_matches_tool_schema.py."""
from src.agents.base_agent import BaseAgent
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS, NOTES, Intent
from src.infrastructure.agent_registry import AgentRegistry

DESCRIPTION = ('Optional: a workspace path (e.g. projects/morning-briefing.md) for the reminder\'s run to use as its '
               'instruction says. Passed here, the path is kept exactly. "remove" takes it off the reminder.')


def test_the_schema_is_exact():
    assert NOTES.context_schemas == {Intent.MANAGE_SELF_REMINDERS: {"workspace_file": DESCRIPTION}}


def test_prose():
    prose = NOTES.capability_descriptions[Intent.MANAGE_SELF_REMINDERS]
    assert "Do not format JSON, do not invent field names" in prose
    assert ('When the reminder\'s run should use a workspace file, pass context={"workspace_file": "<workspace '
            'path>"} and keep the reminder text short, referring to the file.') in prose


def test_the_name_is_unique_across_intents():
    for d in ALL_DESCRIPTORS:
        if d is NOTES:
            continue
        for schema in d.context_schemas.values():
            assert "workspace_file" not in schema


def test_the_model_receives_it():
    registry = AgentRegistry()
    for d in ALL_DESCRIPTORS:
        registry.register(d)
    declaration = BaseAgent._build_delegate_tool_declaration(registry.get_available_intents())
    props = declaration["parameters"]["properties"]["context"]["properties"]
    assert props["workspace_file"] == {"type": "string", "description": DESCRIPTION}
