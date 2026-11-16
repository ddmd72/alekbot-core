"""The email gate's prompt contract (email index v2 §4, §7 gate row, slice 9b): profile `email_classifier` keeps its three
tokens, blueprint `email_classifier_v1` renders them under `EmailGate`; the agent's own prompt carries the code-rendered
reasons block after the template and before the page. The tokens say nothing of the v1 fact (no fact, tags, category,
past tense, body tool), restate no reason value, name the block's two headings, use no placeholder, no Cyrillic and no
example, and hold no time-based keep test (the gate has no Today line)."""
import asyncio
import re
from unittest.mock import Mock

import pytest

from src.agents.email_classification_agent import EmailClassificationAgent
from src.domain.agent import AgentConfig
from src.domain.email_gate import DROP_REASONS, KEEP_REASONS, render_gate_reasons_block
from src.services.prompt_builder import PromptBuilder
from src.services.prompt_v3.context_formatter import ContextFormatter
from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService
from tests.support import prompt_render as pr
from tests.support import prompt_vocabulary as pv
from tests.unit.agents.drive_model_fakes import ScriptedLLM, context, reply
from tests.unit.agents.test_email_classification_gate import PAGE

AGENT_TYPE = "email_classifier"
TOKEN_IDS = ["EMAIL_CLASSIFIER_TAXONOMY", "EMAIL_CLASSIFIER_COGNITIVE_PROCESS", "OUTPUT_FORMAT_EMAIL_CLASSIFIER"]
PROFILE = pv.load_profile(AGENT_TYPE)
TOKENS = pv.load_tokens(PROFILE)
BODIES = {token_id: body for token_id, (_, body) in TOKENS.items()}
ALL = "\n".join(BODIES.values())


def test_the_profile_keeps_its_three_tokens_in_order():
    assert PROFILE["agent_id"] == AGENT_TYPE and PROFILE["blueprint_id"] == "email_classifier_v1"
    assert sorted(PROFILE["tokens"], key=lambda t: PROFILE["tokens"][t]["order"]) == TOKEN_IDS
    assert all(slot["non_overridable"] for slot in PROFILE["tokens"].values())
    assert [TOKENS[t][0] for t in TOKEN_IDS] == ["taxonomy", "cognitive_process", "output_format"]


def test_the_blueprint_is_the_email_gate():
    blueprint = pv.load_blueprint("email_classifier_v1")
    assert blueprint["outer_class"] == "EmailGate extends Agent"
    assert blueprint["class_order"] == ["taxonomy", "cognitive_process", "output_format"]


def test_the_static_template_renders_the_three_sections_with_no_placeholder():
    static = pr.render_static(AGENT_TYPE)
    assert static.startswith("class EmailGate extends Agent {")
    assert static.index("    taxonomy {") < static.index("    cognitive_process {") < static.index("    output_format {")
    assert pr.rendered_token_ids(AGENT_TYPE) == sorted(TOKEN_IDS)
    assert not re.search(r"\{[A-Z][A-Z_]*\}", static)


def _agent_prompt() -> str:
    bio = Mock()
    bio.format.return_value = ""
    bio.format_directives.return_value = ""
    service = PromptAssemblyService(token_repo=pr._Tokens(), blueprint_repo=pr._Blueprints(),
                                    profile_repo=pr._Profiles(), security_port=pr._AllowAll(),
                                    formatter=ContextFormatter(), bio_formatter=bio)
    llm = ScriptedLLM([reply([{"email_id": m.email_id, "decision": "keep", "reason": "unsure"} for m in PAGE])])
    agent = EmailClassificationAgent(AgentConfig(agent_id="email_classifier_u1", agent_type=AGENT_TYPE),
                                     context(llm, "gemini-3.5-flash"), PromptBuilder(repo=None, assembly_service=service))
    asyncio.run(agent.classify_batch(PAGE, "u1", account_id="acc-1"))
    return llm.requests[0].system_instruction


def test_the_agents_prompt_carries_the_reasons_block_after_the_template_and_before_the_page():
    prompt = _agent_prompt()
    block = render_gate_reasons_block()
    assert prompt.index("    output_format {") < prompt.index(block) < prompt.index("Emails to classify:")
    assert not re.search(r"\{[A-Z][A-Z_]*\}", prompt)


@pytest.mark.parametrize("word", ["fact", "tags", "category", "categories", "past tense", "get_email_details",
                                  "valuable", "fetch", "30 days", "today"])
def test_no_token_speaks_of_the_v1_fact_the_body_tool_or_time(word):
    assert word not in ALL.lower()


@pytest.mark.parametrize("value", [*KEEP_REASONS, *DROP_REASONS])
def test_no_reason_value_is_listed_or_defined_in_a_token(value):
    assert not re.search(rf"^\s*-\s*\"?{value}\"?\s*:", ALL, re.M)
    assert not re.search(rf"\b{value}\b\s*[:=(]", ALL)


def test_the_tokens_name_the_blocks_headings_and_the_two_decisions():
    for heading in ("KEEP REASONS", "DROP REASONS"):
        assert heading in render_gate_reasons_block() and heading in BODIES["EMAIL_CLASSIFIER_TAXONOMY"]
    assert '"enum": ["keep", "drop"]' in BODIES["OUTPUT_FORMAT_EMAIL_CLASSIFIER"]
    assert '"required": ["email_id", "decision", "reason"]' in BODIES["OUTPUT_FORMAT_EMAIL_CLASSIFIER"]


def test_no_placeholder_no_cyrillic_and_no_example():
    assert not re.search(r"\{[A-Z][A-Z_]*\}", ALL)
    assert not re.search("[Ѐ-ӿ]", ALL)
    for phrase in ("e.g.", "for example", "for instance", "such as"):
        assert phrase not in ALL.lower()


def test_the_input_fields_the_agent_sends_are_each_explained():
    cognitive = BODIES["EMAIL_CLASSIFIER_COGNITIVE_PROCESS"]
    for field in ("email_id", "subject", "from", "date", "snippet"):
        assert re.search(rf"\b{field}: ", cognitive)
    assert "YYYY-MM-DD" in cognitive and "300" in cognitive and "possibly empty" in cognitive


def test_the_sent_date_is_said_to_be_no_reason_for_either_decision():
    assert "it is no reason for either decision" in BODIES["EMAIL_CLASSIFIER_COGNITIVE_PROCESS"]
