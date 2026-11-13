"""The email card writer's prompt contract (slice 3): the snapshot tokens assemble, through the real PromptAssemblyService
and the agent's own prompt call, into a prompt with both sections, the account's static block after them and every
placeholder filled from code; the numbers written as words equal the code caps; the three-mail variant of the tokens used
by the subscription script cannot drift from the production tokens."""
import asyncio
import re
from pathlib import Path

import pytest

from src.agents.email_card_agent import AGENT_TYPE, EmailCardAgent
from src.domain.agent import AgentConfig
from src.domain.email_card_params import (
    MAIL_DATES_MAX, MAIL_ENTITY_FORMS_MAX, MAIL_LIFE_DOMAINS_MAX, MAIL_TITLE_CHARS, MAIL_WHAT_IT_IS_CHARS,
    MAIL_WORD_LIST_MAX,
)
from src.domain.email_card_prompt import card_placeholder_values, render_card_prompt, render_card_static_block
from src.ports.llm_port import PROMPT_CACHE_BOUNDARY
from src.services.prompt_builder import PromptBuilder
from src.services.prompt_v3.context_formatter import ContextFormatter
from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService
from tests.support import prompt_render as pr
from tests.support import prompt_vocabulary as pv
from tests.unit.agents.drive_model_fakes import ScriptedLLM, context
from tests.unit.domain.email_card_fixtures import PEOPLE

X3_DIR = Path(__file__).resolve().parents[3] / "scripts" / "email" / "card_prompts_x3"
WORDS = {3: "three", 5: "five", 12: "twelve"}


def _tokens():
    return pv.load_tokens(pv.load_profile(AGENT_TYPE))


def _body(token_id: str) -> str:
    return _tokens()[token_id][1]


def _agent() -> EmailCardAgent:
    from unittest.mock import Mock
    bio = Mock()
    bio.format.return_value = ""
    bio.format_directives.return_value = ""
    service = PromptAssemblyService(token_repo=pr._Tokens(), blueprint_repo=pr._Blueprints(), profile_repo=pr._Profiles(),
                                    security_port=pr._AllowAll(), formatter=ContextFormatter(), bio_formatter=bio)
    return EmailCardAgent(AgentConfig(agent_id=AGENT_TYPE, agent_type=AGENT_TYPE),
                          context(ScriptedLLM([]), "gemini-3.5-flash-lite"),
                          PromptBuilder(repo=None, assembly_service=service))


def test_the_profile_and_blueprint_name_both_tokens_in_order():
    profile = pv.load_profile(AGENT_TYPE)
    assert profile["agent_id"] == AGENT_TYPE and list(profile["tokens"]) == [
        "COGNITIVE_PROCESS_EMAIL_CARD", "OUTPUT_FORMAT_EMAIL_CARD"]
    blueprint = pv.load_blueprint(profile["blueprint_id"])
    assert blueprint["class_order"] == ["cognitive_process", "output_format"]
    assert blueprint["outer_class"] == "EmailCardAgent extends Agent"
    assert all(slot["non_overridable"] for slot in profile["tokens"].values())


def test_the_output_format_holds_exactly_the_seven_placeholders_and_the_cognitive_token_none():
    found = set(re.findall(r"\{([A-Z][A-Z_]*)\}", _body("OUTPUT_FORMAT_EMAIL_CARD")))
    assert found == set(card_placeholder_values())
    assert re.findall(r"\{[A-Z][A-Z_]*\}", _body("COGNITIVE_PROCESS_EMAIL_CARD")) == []


def test_the_static_template_assembles_both_tokens():
    static = pr.render_static(AGENT_TYPE)
    assert static.startswith("class EmailCardAgent extends Agent {")
    assert static.index("    cognitive_process {") < static.index("    output_format {")


def test_the_agents_own_prompt_is_rendered_and_carries_the_account_block_after_the_class():
    block = render_card_static_block(PEOPLE, "alex@example.com")
    prompt = asyncio.run(_agent()._system_prompt(block))
    assert not re.search(r"\{[A-Z][A-Z_]*\}", prompt)
    assert "doc_type: one of " + card_placeholder_values()["DOC_TYPES"] in prompt
    assert prompt.index("    cognitive_process {") < prompt.index("    output_format {") < prompt.index(block)
    assert prompt.split(PROMPT_CACHE_BOUNDARY)[0].rstrip().endswith(block)


def test_a_placeholder_left_after_rendering_would_be_refused():
    with pytest.raises(ValueError):
        render_card_prompt(_body("OUTPUT_FORMAT_EMAIL_CARD") + " {NOT_A_LIST}")


def test_no_cyrillic_and_no_examples_in_the_tokens():
    for token_id in ("COGNITIVE_PROCESS_EMAIL_CARD", "OUTPUT_FORMAT_EMAIL_CARD"):
        body = _body(token_id)
        assert not re.search("[Ѐ-ӿ]", body)
        for phrase in ("e.g.", "for example", "for instance"):
            assert phrase not in body.lower()


@pytest.mark.parametrize("phrase", [
    f"at most {WORDS[MAIL_ENTITY_FORMS_MAX]}", f"one to {WORDS[MAIL_LIFE_DOMAINS_MAX]}",
    f"at most {MAIL_WHAT_IT_IS_CHARS} characters", f"at most {MAIL_TITLE_CHARS} characters",
    f"at most {WORDS[MAIL_WORD_LIST_MAX]}", f"each word list at most {WORDS[MAIL_WORD_LIST_MAX]}",
    f"dates at most {WORDS[MAIL_DATES_MAX]}",
])
def test_the_numbers_written_as_words_equal_the_code_caps(phrase):
    assert phrase in _body("COGNITIVE_PROCESS_EMAIL_CARD")


def _x3(name: str) -> str:
    return (X3_DIR / name).read_text(encoding="utf-8").strip("\n")


@pytest.mark.parametrize("token_id, x3_name", [("COGNITIVE_PROCESS_EMAIL_CARD", "COGNITIVE_PROCESS_EMAIL_CARD_X3.txt"),
                                                ("OUTPUT_FORMAT_EMAIL_CARD", "OUTPUT_FORMAT_EMAIL_CARD_X3.txt")])
def test_the_three_mail_tokens_differ_only_in_the_identity_input_format_and_shape_lines(token_id, x3_name):
    production, three = _body(token_id).strip("\n").splitlines(), _x3(x3_name).splitlines()
    assert len(production) == len(three)
    differing = [p.split(":", 1)[0] for p, t in zip(production, three) if p != t]
    assert set(differing) <= {"identity", "input", "format", "shape"} and differing
    for p, t in zip(production, three):
        if p != t:
            assert p.split(":", 1)[0] == t.split(":", 1)[0]


def test_the_three_mail_tokens_keep_the_same_rules_and_placeholders():
    assert set(re.findall(r"\{([A-Z][A-Z_]*)\}", _x3("OUTPUT_FORMAT_EMAIL_CARD_X3.txt"))) == set(card_placeholder_values())
    assert not re.search("[Ѐ-ӿ]", _x3("COGNITIVE_PROCESS_EMAIL_CARD_X3.txt") + _x3("OUTPUT_FORMAT_EMAIL_CARD_X3.txt"))
