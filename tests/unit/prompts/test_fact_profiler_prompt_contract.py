"""The fact profiler's prompt contract (FACT_PROFILE_RFC §4.3, §10.2): the snapshot files assemble, through the real
PromptAssemblyService and the agent's own prompt call, into the one static prompt the measurement approved (pinned by
hash); the OUTPUT_FORMAT's field list, the response schema and `ProfileAnswer` name the same fields and values; the
enums come from the registry; the blueprint and profile fit the tokens; the validators reject what the schema forbids.

Not under tests/contracts/ (RFC §9 names it there): `make check` and `make ci` run tests/unit/ only."""
import hashlib
import re
from typing import Dict
from unittest.mock import AsyncMock, Mock

import pytest

from src.agents.fact_profiler_agent import AGENT_TYPE, FactProfilerAgent, response_schema
from src.domain.agent import AgentConfig
from src.domain.fact_domains import render_subject_enum_block, render_use_area_line, subject_enum, use_area_enum
from src.domain.fact_profile_answer import PROFILE_KINDS, ProfileAnswer
from src.domain.prompt_v3.agent_profile import AgentProfile
from src.domain.prompt_v3.blueprint import Blueprint
from src.domain.prompt_v3.profile_slot import ProfileToken
from src.domain.prompt_v3.token import Token, TokenCategory, TokenClass, TokenId
from src.ports.llm_port import PROMPT_CACHE_BOUNDARY
from src.services.prompt_builder import PromptBuilder
from src.services.prompt_v3.context_formatter import ContextFormatter
from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService
from tests.support.prompt_vocabulary import load_blueprint, load_profile, load_tokens
from tests.unit.agents.drive_model_fakes import ScriptedLLM, context

# The static part of the assembled prompt (everything before the cache boundary), as the model receives it.
STATIC_SHA256 = "e5aa2e85904097c9c9831d6bde1a3e26c48cb8e8c6a4dd519848e2f397c348ef"

ANSWER = {
    "label_words": ["car cover"], "time_role": "validity",
    "time_points": [{"from": "2026-04-01", "to": "2027-03-31", "precision": "day", "source": "01/04/2026"}],
    "places": [{"city": "Rome", "country": "Italy", "source": "Rome"}],
    "people": [{"name": "Clara Moreira", "role": "partner", "source": "Clara"}],
    "tax_or_fine": "none", "user_identity": False, "pet": False, "state_or_law": False, "about_person": "none",
    "subject": "transport", "money_flow": True, "use_areas": ["money"], "kinds": ["document"],
    "sensitivity": "ordinary",
}


def _profile() -> dict:
    return load_profile(AGENT_TYPE)


def _tokens() -> Dict[str, tuple]:
    return load_tokens(_profile())


def _service() -> PromptAssemblyService:
    profile, blueprint, tokens = _profile(), load_blueprint(_profile()["blueprint_id"]), _tokens()
    docs = {tid: Token(id=TokenId(tid), category=TokenCategory(cls), class_=TokenClass(cls), content=body,
                       metadata={}) for tid, (cls, body) in tokens.items()}

    class Tokens:
        async def get(self, token_id): return docs[str(token_id)]

    class Blueprints:
        async def get(self, blueprint_id):
            return Blueprint(id=blueprint["blueprint_id"], outer_class=blueprint["outer_class"],
                             class_order=blueprint["class_order"])

    class Profiles:
        async def get_agent_profile(self, agent_id):
            return AgentProfile(blueprint_id=blueprint["blueprint_id"], tokens={
                tid: ProfileToken(tid, slot["order"], non_overridable=slot["non_overridable"])
                for tid, slot in profile["tokens"].items()})

        async def get_override_tokens(self, owner_type, owner_id): return {}

    security = Mock()

    def _allow(text, **_):
        result = Mock()
        result.sanitized_text = text
        result.risk_level = Mock(value="LOW")
        return result

    security.validate = AsyncMock(side_effect=_allow)
    bio = Mock()
    bio.format = Mock(return_value="")
    bio.format_directives = Mock(return_value="")
    return PromptAssemblyService(token_repo=Tokens(), blueprint_repo=Blueprints(), profile_repo=Profiles(),
                                 security_port=security, formatter=ContextFormatter(), bio_formatter=bio)


async def _agent_prompt() -> str:
    """The prompt the agent itself builds: its own call into the real builder over the snapshot files."""
    builder = PromptBuilder(repo=None, assembly_service=_service())
    agent = FactProfilerAgent(AgentConfig(agent_id=AGENT_TYPE, agent_type=AGENT_TYPE),
                              context(ScriptedLLM([]), "claude-sonnet-5-5"), builder)
    return await agent._system_prompt()


def _fields_line(name: str) -> str:
    """The line of OUTPUT_FORMAT's `Fields:` list that names `name` (a bullet may name several fields)."""
    body = _tokens()["OUTPUT_FORMAT_FACT_PROFILER"][1]
    block = body.split("Fields:\n", 1)[1].split("\nThe last ten", 1)[0]
    bullets = re.split(r"\n(?=- )", block)
    for bullet in bullets:
        head = bullet[2:].split(":", 1)[0]
        if name in [n.strip() for n in head.split(",")]:
            return " ".join(bullet.split())
    raise AssertionError(f"{name} is not in the Fields list")


@pytest.mark.asyncio
async def test_the_static_prompt_is_pinned():
    prompt = await _agent_prompt()
    static = prompt.split(PROMPT_CACHE_BOUNDARY)[0]
    assert hashlib.sha256(static.encode("utf-8")).hexdigest() == STATIC_SHA256


@pytest.mark.asyncio
async def test_the_prompt_carries_both_sections_then_the_registry_enum_block_after_the_class():
    prompt = await _agent_prompt()
    static, dynamic = prompt.split(PROMPT_CACHE_BOUNDARY)
    assert dynamic.strip() == ""
    assert static.startswith("class FactProfilerAgent extends Agent {")
    enum_block = render_subject_enum_block() + "\n" + render_use_area_line()
    assert static.rstrip().endswith(enum_block)
    assert static.index("    cognitive_process {") < static.index("    output_format {") < static.index(enum_block)
    assert static.index("\n}\n") < static.index(enum_block)   # the block follows the closed class, at column 0


@pytest.mark.asyncio
async def test_no_cyrillic_and_no_examples_in_the_prompt():
    prompt = await _agent_prompt()
    assert not re.search("[Ѐ-ӿ]", prompt)
    for phrase in ("e.g.", "for example", "for instance"):
        assert phrase not in prompt.lower()


def test_the_fields_list_the_schema_and_the_answer_model_name_the_same_fields():
    schema = response_schema()
    declared = list(ProfileAnswer.model_fields)
    assert list(schema["properties"]) == declared and schema["required"] == declared
    listed = [n.strip() for bullet in _fields_bullets() for n in bullet.split(":", 1)[0].split(",")]
    assert sorted(listed) == sorted(declared)


def _fields_bullets():
    body = _tokens()["OUTPUT_FORMAT_FACT_PROFILER"][1]
    block = body.split("Fields:\n", 1)[1].split("\nThe last ten", 1)[0]
    return [b[2:] for b in re.split(r"\n(?=- )", block)]


@pytest.mark.parametrize("field, nested", [
    ("time_points", ["from", "to", "precision", "source"]),
    ("places", ["city", "country", "source"]),
    ("people", ["name", "role", "source"]),
])
def test_each_nested_property_is_named_in_its_line_and_is_in_the_schema(field, nested):
    items = response_schema()["properties"][field]["items"]
    assert list(items["properties"]) == nested and items["required"] == nested
    assert items["additionalProperties"] is False
    line = _fields_line(field)
    assert all(re.search(rf"\b{name}\b", line) for name in nested)


@pytest.mark.parametrize("field", ["time_role", "tax_or_fine", "about_person", "sensitivity"])
def test_a_closed_fields_values_in_its_line_are_the_schema_enum(field):
    values = response_schema()["properties"][field]["enum"]
    line = _fields_line(field)
    listed = re.split(r",\s*", re.search(r"one of ([a-z, ]+?)(?: \(|\.|$)", line).group(1))
    assert listed == values


def test_the_kinds_line_lists_the_schema_kinds():
    line = _fields_line("kinds")
    listed = re.split(r",\s*", re.search(r"among ([a-z, ]+?)\.?$", line).group(1))
    assert listed == list(PROFILE_KINDS) == response_schema()["properties"]["kinds"]["items"]["enum"]


def test_the_schema_enums_are_the_registry_enums():
    props = response_schema()["properties"]
    assert props["subject"]["enum"] == list(subject_enum())
    assert props["use_areas"]["items"]["enum"] == list(use_area_enum())
    block = render_subject_enum_block()
    assert all(re.search(rf"^{key}: ", block, re.M) for key in subject_enum())
    assert all(key in render_use_area_line() for key in use_area_enum() if key not in subject_enum())


def test_every_property_is_required_and_every_object_closed_with_no_descriptions():
    def objects(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                yield node
            for value in node.values():
                yield from objects(value)
        elif isinstance(node, list):
            for value in node:
                yield from objects(value)

    schema = response_schema()
    for obj in objects(schema):
        assert obj["additionalProperties"] is False and obj["required"] == list(obj["properties"])
    assert "description" not in str(schema)


def test_the_blueprint_validates_and_the_profiles_tokens_fit_its_classes():
    profile = _profile()
    blueprint = load_blueprint(profile["blueprint_id"])
    assert Blueprint(id=blueprint["blueprint_id"], outer_class=blueprint["outer_class"],
                     class_order=blueprint["class_order"]).class_order == ["cognitive_process", "output_format"]
    assert blueprint["outer_class"] == "FactProfilerAgent extends Agent"
    tokens = _tokens()
    assert set(tokens) == {"COGNITIVE_PROCESS_FACT_PROFILER", "OUTPUT_FORMAT_FACT_PROFILER"}
    assert {cls for cls, _ in tokens.values()} == set(blueprint["class_order"])
    assert all(slot == {"non_overridable": True, "order": 10} for slot in profile["tokens"].values())
    assert profile["agent_id"] == AGENT_TYPE


def test_a_valid_answer_passes_and_a_missing_field_or_out_of_enum_value_fails():
    assert ProfileAnswer.model_validate(ANSWER).subject == "transport"
    with pytest.raises(ValueError):
        ProfileAnswer.model_validate({k: v for k, v in ANSWER.items() if k != "kinds"})
    for key, bad in [("subject", "astrology"), ("use_areas", ["astrology"]), ("kinds", ["feeling"]),
                     ("time_role", "sometime"), ("sensitivity", "secret")]:
        with pytest.raises(ValueError):
            ProfileAnswer.model_validate({**ANSWER, key: bad})
