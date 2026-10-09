"""Every field a capability description tells the model to send must exist in the tool schema.

Why (2026-10-07 and 2026-10-09 incidents): `delegate_to_specialist.context` is built from
`context_schemas` only. OpenAI applies strict function grammar by default, so a key the prose
asks for but the schema does not declare is a masked token — the model emitted whitespace
until max_output_tokens (64k, ~13 min) instead. deep_research named `language`/`brief` in prose
with no `context_schemas`; a replay of the stored request reproduced the runaway 4/45 times and
0/45 once both keys were declared. See
docs/04_solution_strategy/decisions/delegate_tool_contract_and_strict.md.

`query` is a top-level tool parameter, not a context field.
"""
import re

import pytest

from src.agents.base_agent import BaseAgent
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS
from src.infrastructure.agent_registry import AgentRegistry

# `payload: {"a": ..., "b": ...}` and `context={"a": ...}` / `context: {"a": ...}` blocks.
_BLOCK = re.compile(r'(?:payload|context)\s*[:=]\s*\{([^{}]*)\}')
_KEY = re.compile(r'"(\w+)"\s*:')
_TOP_LEVEL = {"query"}


def _prose_fields(text: str) -> set:
    fields: set = set()
    for block in _BLOCK.findall(text):
        fields.update(_KEY.findall(block))
    return fields - _TOP_LEVEL


_CASES = [
    pytest.param(d, intent, text, id=f"{d.agent_id}:{intent}")
    for d in ALL_DESCRIPTORS
    for intent, text in d.capability_descriptions.items()
    if _prose_fields(text)
]


def _generated_context_properties(descriptor) -> set:
    """The `context` properties the model actually receives for this agent's intents."""
    registry = AgentRegistry()
    registry.register(descriptor)
    declaration = BaseAgent._build_delegate_tool_declaration(registry._describe(include_internal=True))
    return set(declaration["parameters"]["properties"]["context"].get("properties", {}))


@pytest.mark.parametrize("descriptor,intent,text", _CASES)
def test_every_prose_field_is_in_the_generated_tool_schema(descriptor, intent, text):
    generated = _generated_context_properties(descriptor)
    missing = _prose_fields(text) - generated
    assert not missing, (
        f"{descriptor.agent_id}/{intent}: prose names {sorted(missing)} but the generated "
        f"delegate_to_specialist.context has {sorted(generated)} — declare them in context_schemas"
    )


def test_prose_parser_finds_fields():
    """Guard the parser itself, so an empty match set cannot pass the test above vacuously."""
    text = 'payload: {"query": "<q>", "language": "<l>", "brief": "<b>"}'
    assert _prose_fields(text) == {"language", "brief"}
    assert _prose_fields('context={"email_id": "<id>"}') == {"email_id"}
    assert _CASES, "no capability description names a payload field — parser regressed"
