"""The email gate agent (email index v2 §4, §6.2, slice 9b): one call in JSON mode with no tools, the code-rendered
reasons block in its system prompt, a neutral user line; a fenced answer is invalid JSON and gets the standard parse
retry (the code-block exception is gone); two bad answers fail the page; the code rule decides over a valid answer."""
import json
from datetime import datetime
from typing import List, Optional

import pytest

from src.agents.email_classification_agent import EmailClassificationAgent
from src.domain.agent import AgentConfig
from src.domain.email import EmailClassificationResult, EmailMetadata
from src.domain.email_gate import render_gate_reasons_block
from src.domain.exceptions import BothProvidersUnavailableError, LLMError
from tests.unit.agents.drive_model_fakes import ScriptedLLM, context, reply

TEMPLATE = "class EmailGate extends Agent {\n\n    taxonomy {\n\n    rules\n\n    }\n\n}"


class _Prompts:
    """A prompt builder that appends the static blocks after the template, as the assembly service does, and keeps the
    options each prompt was built with."""

    def __init__(self) -> None:
        self.options: List[dict] = []

    async def build_for_agent(self, agent_type: str, extra_static_blocks: Optional[List[str]] = None, **options) -> str:
        self.options.append(options)
        return "\n\n".join([TEMPLATE, *(extra_static_blocks or [])])


def _mail(email_id: str, subject: str, sender: str, snippet: str) -> EmailMetadata:
    return EmailMetadata(email_id=email_id, provider="gmail", subject=subject, from_address=sender,
                         date=datetime(2026, 8, 3, 9, 15), labels=["INBOX"], snippet=snippet)


PAGE = [
    _mail("msg_rome01", "Booking RM-55120 confirmed", "Albergo Immaginario <stay@albergo.example>",
          "Dear Alex Doe, your stay at Albergo Immaginario, Via Finta 7, Rome is confirmed."),
    _mail("msg_shop03", "This week's offers", "Supermercado Exemplo <news@supermercado.example>", "x" * 400),
]


def _agent(llm: ScriptedLLM, prompts: Optional[_Prompts] = None) -> EmailClassificationAgent:
    return EmailClassificationAgent(AgentConfig(agent_id="email_classifier_u1", agent_type="email_classifier"),
                                    context(llm, "gemini-3.5-flash"), prompts or _Prompts(), user_id="u1")


GOOD = [{"email_id": "msg_rome01", "decision": "keep", "reason": "record"},
        {"email_id": "msg_shop03", "decision": "drop", "reason": "promotion"}]


async def test_one_request_in_json_mode_with_no_tools_decides_the_page():
    llm = ScriptedLLM([reply(GOOD)])

    results = await _agent(llm).classify_batch(PAGE, "u1", account_id="acc-1")

    assert results == [EmailClassificationResult("msg_rome01", keep=True, reason="record"),
                       EmailClassificationResult("msg_shop03", keep=False, reason="promotion")]
    (request,) = llm.requests
    assert request.response_mime_type == "application/json"
    assert not request.tools and request.thinking == "medium"


async def test_the_reasons_block_and_the_page_are_in_the_system_prompt():
    llm = ScriptedLLM([reply(GOOD)])

    await _agent(llm).classify_batch(PAGE, "u1", account_id="acc-1")

    system = llm.requests[0].system_instruction
    assert render_gate_reasons_block() in system
    assert system.index(render_gate_reasons_block()) < system.index("Emails to classify:")
    page = json.loads(system.split("Emails to classify:\n", 1)[1])
    assert [m["email_id"] for m in page] == ["msg_rome01", "msg_shop03"]
    assert set(page[0]) == {"email_id", "subject", "from", "date", "snippet"}
    assert page[0]["date"] == "2026-08-03" and len(page[1]["snippet"]) == 300


async def test_the_user_line_is_neutral():
    llm = ScriptedLLM([reply(GOOD)])

    await _agent(llm).classify_batch(PAGE, "u1", account_id="acc-1")

    (message,) = llm.requests[0].messages
    text = message.parts[0].text
    assert message.role == "user" and "(2 in all)" in text
    for word in ("ruthless", "most will not", "system", "memory"):
        assert word not in text.lower()


async def test_the_prompt_carries_no_owner_facts_directives_or_location():
    prompts = _Prompts()

    await _agent(ScriptedLLM([reply(GOOD)]), prompts).classify_batch(PAGE, "u1", account_id="acc-1")

    (options,) = prompts.options
    assert options["include_biographical"] is False and options["include_directives"] is False
    assert options["include_location"] is False and options["account_id"] == "acc-1"


async def test_a_fenced_answer_gets_the_parse_retry():
    llm = ScriptedLLM([reply(text="```json\n" + json.dumps(GOOD) + "\n```"), reply(GOOD)])

    results = await _agent(llm).classify_batch(PAGE, "u1", account_id="acc-1")

    assert [r.keep for r in results] == [True, False] and not any(r.failure for r in results)
    assert len(llm.requests) == 2
    retry = llm.requests[1].messages
    assert [m.role for m in retry] == ["user", "model", "user"]
    assert "output_format" in retry[2].parts[0].text


@pytest.mark.parametrize("bad", ['{"email_id": "msg_rome01"}', '["msg_rome01"]', "not json"])
async def test_two_bad_answers_fail_the_page(bad):
    llm = ScriptedLLM([reply(text=bad), reply(text=bad)])

    results = await _agent(llm).classify_batch(PAGE, "u1", account_id="acc-1")

    assert results == [EmailClassificationResult("msg_rome01", keep=False, failure="parse_error"),
                       EmailClassificationResult("msg_shop03", keep=False, failure="parse_error")]
    assert len(llm.requests) == 2


async def test_the_code_rule_keeps_what_the_answer_does_not_settle():
    llm = ScriptedLLM([reply([{"email_id": "msg_shop03", "decision": "drop", "reason": "record"},
                              {"email_id": "msg_elsewhere", "decision": "drop", "reason": "promotion"}])])

    results = await _agent(llm).classify_batch(PAGE, "u1", account_id="acc-1")

    assert results == [EmailClassificationResult("msg_rome01", keep=True, reason=None, forced=True),
                       EmailClassificationResult("msg_shop03", keep=True, reason="record", forced=True)]


async def test_a_provider_outage_propagates_and_another_error_fails_the_page():
    outage = BothProvidersUnavailableError("gemini", None, LLMError("down", http_status=503))
    with pytest.raises(BothProvidersUnavailableError):
        await _agent(ScriptedLLM([outage])).classify_batch(PAGE, "u1", account_id="acc-1")

    results = await _agent(ScriptedLLM([RuntimeError("boom")])).classify_batch(PAGE, "u1", account_id="acc-1")
    assert [(r.keep, r.failure) for r in results] == [(False, "RuntimeError"), (False, "RuntimeError")]


async def test_an_empty_page_makes_no_call():
    llm = ScriptedLLM([])
    assert await _agent(llm).classify_batch([], "u1", account_id="acc-1") == []
    assert llm.requests == []
