"""The summarizer's output contract: one {title, summary} JSON object, parsed with json.loads, retried once on a bad
output, then a parse_error failure (src/adapters/CLAUDE.md, Agent Output Format Standards)."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.lelik_summarizer_agent import _RESPONSE_SCHEMA, LelikSummarizerAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.companion_extraction import EXTRACTION_TASK
from src.domain.user import PerformanceTier
from src.ports.llm_port import AgentExecutionContext, LLMPort, ProviderCapabilities
from src.ports.prompt_builder_port import PromptBuilderPort

_GOOD = json.dumps({"title": " Check-in at Albergo Immaginario ", "summary": " Alex Doe checked in at Albergo Immaginario with booking RM-55120. "})


def _reply(text):
    return MagicMock(text=text, usage_metadata=MagicMock(total_tokens=7))


def _agent(texts):
    llm = AsyncMock(spec=LLMPort)
    llm.generate_content.side_effect = [_reply(t) for t in texts]
    pb = AsyncMock(spec=PromptBuilderPort)
    pb.build_for_agent.return_value = "You are Lelik's end-of-call summarizer."
    config = AgentConfig(agent_id="lelik_summarizer_test", agent_type="lelik_summarizer",
                         timeout_ms=60000, capabilities=["lelik_summarizer"])
    ctx = AgentExecutionContext(
        agent_type="lelik_summarizer", provider=llm, model_name="claude-sonnet-5", tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities(), provider_name="claude", resilience_port=InMemoryProviderResilience(),
    )
    return LelikSummarizerAgent(config=config, execution_context=ctx, prompt_builder=pb), llm


def _request(call):
    """The LLMRequest of one provider call, whether it was passed by position or by keyword."""
    return call.args[0] if call.args else next(iter(call.kwargs.values()))


def _message():
    return AgentMessage(
        task_id="t1", sender="test", recipient="lelik_summarizer_test", intent=AgentIntent.DELEGATE,
        payload={"task": EXTRACTION_TASK, "messages": [
            {"request_text": "We have a booking under RM-55120.", "response_text": "Abbiamo una prenotazione con il codice RM-55120."}]},
        context={"account_id": "acc-1"},
    )


async def test_valid_json_gives_records_title_and_summary():
    agent, llm = _agent([_GOOD])
    response = await agent.execute(_message())
    assert response.status is AgentStatus.SUCCESS
    assert response.result == {"records": [], "title": "Check-in at Albergo Immaginario", "summary": "Alex Doe checked in at Albergo Immaginario with booking RM-55120."}
    assert llm.generate_content.await_count == 1


@pytest.mark.parametrize("bad", ["```json\n" + _GOOD + "\n```", "Alex Doe checked in at Albergo Immaginario with booking RM-55120."])
async def test_a_bad_output_is_retried_with_the_output_and_a_correction(bad):
    agent, llm = _agent([bad, _GOOD])
    response = await agent.execute(_message())
    assert response.status is AgentStatus.SUCCESS
    assert response.result["title"] == "Check-in at Albergo Immaginario"
    assert llm.generate_content.await_count == 2
    second = _request(llm.generate_content.await_args_list[1])
    assert [m.role for m in second.messages] == ["user", "model", "user"]
    assert second.messages[1].parts[0].text == bad
    assert "not valid JSON" in second.messages[2].parts[0].text


async def test_two_invalid_outputs_fail_after_exactly_two_calls():
    agent, llm = _agent(["nope", "still nope"])
    response = await agent.execute(_message())
    assert response.status is AgentStatus.FAILED
    assert response.error == "parse_error"
    assert llm.generate_content.await_count == 2


@pytest.mark.parametrize("payload", [
    {"summary": "We asked."},
    {"title": "Coimbra", "summary": 3},
    ["title", "summary"],
])
async def test_a_wrong_shape_is_retried(payload):
    agent, llm = _agent([json.dumps(payload), _GOOD])
    response = await agent.execute(_message())
    assert response.status is AgentStatus.SUCCESS
    assert llm.generate_content.await_count == 2


async def test_the_request_carries_the_schema_and_json_mime():
    agent, llm = _agent([_GOOD])
    await agent.execute(_message())
    request = _request(llm.generate_content.await_args)
    assert request.response_schema == _RESPONSE_SCHEMA
    assert request.response_mime_type == "application/json"
