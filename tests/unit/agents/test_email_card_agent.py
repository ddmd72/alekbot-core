"""EmailCardAgent (slice 3): one structured call per mail on a scripted provider; the statuses, the parse retry with the
correction turn, the per-block system cache, the missing-token outage that heals after an upload, the request shape."""
import json

import pytest

from src.agents.email_card_agent import AGENT_TYPE, EmailCardAgent
from src.domain.agent import AgentConfig
from src.domain.email_card_prompt import card_response_schema, card_task_line
from src.domain.email_card_reply import CardWriterStatus
from src.domain.exceptions import LLMAccountError, LLMClientError, LLMRefusalError, LLMServerError
from src.domain.llm import FinishReason
from tests.unit.agents.drive_model_fakes import ScriptedLLM, context, prompt_builder, reply
from tests.unit.domain.email_card_fixtures import answer

BLOCK = "OWNER AND CONFIRMED PEOPLE\nThe owner: Alex Doe"
INPUT = "<mail>Prenotazione RM-55120</mail>"
PROMPT = ("class EmailCardAgent extends Agent {\n    cognitive_process {\n    rules\n    }\n"
          "    output_format {\n    doc_type: one of {DOC_TYPES}. about: {ABOUT_VALUES}.\n    }\n}")
BAD = {"language": "it"}


def agent(script, builder=None, model="gemini-3.5-flash-lite"):
    llm = ScriptedLLM(script)
    builder = builder or prompt_builder(PROMPT)
    return EmailCardAgent(AgentConfig(agent_id=AGENT_TYPE, agent_type=AGENT_TYPE), context(llm, model), builder), llm, builder


async def test_a_valid_answer_is_ok_with_the_pinned_model_and_one_call():
    a, llm, _ = agent([reply(answer())])
    out = await a.write("acc1", BLOCK, INPUT)
    assert out.status is CardWriterStatus.OK and out.calls == 1 and out.answer.doc_type == "booking"
    assert out.model == "gemini-3.5-flash-lite" and out.usd >= 0


async def test_the_request_carries_json_the_schema_low_thinking_no_storage_and_the_task_line():
    a, llm, _ = agent([reply(answer())])
    await a.write("acc1", BLOCK, INPUT)
    req = llm.requests[0]
    assert req.response_mime_type == "application/json" and req.response_schema == card_response_schema()
    assert req.thinking == "low" and req.allow_provider_storage is False and req.max_tokens == 6_000
    assert req.messages[0].parts[0].text == f"{INPUT}\n\n{card_task_line()}"
    assert req.messages[0].parts[0].text.endswith(card_task_line())


async def test_an_invalid_answer_gets_one_correction_turn_then_succeeds():
    a, llm, _ = agent([reply(BAD), reply(answer())])
    out = await a.write("acc1", BLOCK, INPUT)
    assert out.status is CardWriterStatus.OK and out.calls == 2
    turns = llm.requests[1].messages
    assert [m.role for m in turns] == ["user", "model", "user"]
    assert turns[1].parts[0].text == json.dumps(BAD) and "not valid" in turns[2].parts[0].text


async def test_invalid_twice_is_invalid():
    a, llm, _ = agent([reply(BAD), reply(BAD)])
    out = await a.write("acc1", BLOCK, INPUT)
    assert out.status is CardWriterStatus.INVALID and out.answer is None and out.calls == 2


async def test_the_output_limit_is_invalid_and_the_safety_filter_is_refused():
    a, _, _ = agent([reply(answer(), finish=FinishReason.MAX_TOKENS)])
    assert (await a.write("acc1", BLOCK, INPUT)).status is CardWriterStatus.INVALID
    a, _, _ = agent([reply(answer(), finish=FinishReason.SAFETY)])
    assert (await a.write("acc1", BLOCK, INPUT)).status is CardWriterStatus.REFUSED


@pytest.mark.parametrize("error, status", [
    (LLMRefusalError("declined"), CardWriterStatus.REFUSED),
    (LLMClientError("bad request", http_status=400), CardWriterStatus.REFUSED),
    (LLMClientError("rate", http_status=429), CardWriterStatus.UNAVAILABLE),
    (LLMServerError("down", http_status=503), CardWriterStatus.UNAVAILABLE),
    (LLMAccountError("billing", http_status=400), CardWriterStatus.UNAVAILABLE),
    (TimeoutError("slow"), CardWriterStatus.UNAVAILABLE),
])
async def test_provider_errors_map_to_a_status(error, status):
    a, _, _ = agent([error] * 3)
    assert (await a.write("acc1", BLOCK, INPUT)).status is status


async def test_a_prompt_missing_a_section_is_an_outage_and_the_next_write_after_the_upload_assembles_afresh():
    builder = prompt_builder("class EmailCardAgent extends Agent {\n    cognitive_process {\n    rules\n    }\n}")
    a, llm, _ = agent([reply(answer())], builder=builder)
    assert (await a.write("acc1", BLOCK, INPUT)).status is CardWriterStatus.UNAVAILABLE
    assert llm.requests == []
    builder.invalidate_agent_prompt.assert_called_once_with(AGENT_TYPE)
    builder.build_for_agent.return_value = PROMPT          # the tokens are uploaded
    out = await a.write("acc1", BLOCK, INPUT)
    assert out.status is CardWriterStatus.OK and builder.build_for_agent.await_count == 2


async def test_a_placeholder_code_cannot_fill_is_an_outage():
    builder = prompt_builder(PROMPT.replace("{ABOUT_VALUES}", "{ABOUT_VALUE}"))
    a, llm, _ = agent([reply(answer())], builder=builder)
    assert (await a.write("acc1", BLOCK, INPUT)).status is CardWriterStatus.UNAVAILABLE
    assert llm.requests == [] and builder.invalidate_agent_prompt.called


async def test_an_assembly_error_is_an_outage():
    a, _, _ = agent([reply(answer())], builder=prompt_builder(error=RuntimeError("firestore down")))
    assert (await a.write("acc1", BLOCK, INPUT)).status is CardWriterStatus.UNAVAILABLE


async def test_the_system_is_rendered_holds_the_block_and_is_assembled_once_per_block():
    a, llm, builder = agent([reply(answer())] * 4)
    await a.write("acc1", BLOCK, INPUT)
    await a.write("acc1", BLOCK, INPUT)
    await a.write("acc2", BLOCK + " and Clara", INPUT)
    assert builder.build_for_agent.await_count == 2
    kwargs = builder.build_for_agent.await_args_list[0].kwargs
    assert kwargs["extra_static_blocks"] == [BLOCK] and kwargs["agent_type"] == AGENT_TYPE
    assert kwargs["include_biographical"] is False and kwargs["include_directives"] is False
    system = llm.requests[0].system_instruction
    assert "{DOC_TYPES}" not in system and "booking" in system and system is not None
    assert llm.requests[0].system_instruction == llm.requests[1].system_instruction


async def test_the_agent_is_not_a_delegation_agent():
    a, _, _ = agent([])
    assert await a.can_handle(None) is False
