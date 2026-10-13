"""Search v2 P3 review fixes in the drive agents (USER_DRIVE_INDEXING_RFC §25.4.3, §25.4.4, §25.4.6):
- M2: a safety block the FALLBACK serves as a normal response (Gemini) is a charged outage retried on the primary,
  never a refusal of the input; the primary's own safety stop is still a refusal;
- m6: Claude's safety-classifier decline is a refusal of the input, not invalid output;
- m1: identical images still disagreeing after the correction turn are settled as text (the reader looks anyway);
- m2: a triage correction turn stopped at the cap keeps the answers already received."""
from types import SimpleNamespace

import pytest

from src.adapters.claude_adapter import ClaudeAdapter
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.base_agent import BaseAgent
from src.agents.drive_image_triage_agent import DriveImageTriageAgent, TriageInput
from src.agents.drive_passage_agent import DrivePassageAgent
from src.agents.drive_profiler_agent import DriveProfilerAgent
from src.domain.agent import AgentConfig
from src.domain.drive_enrichment_calls import ModelReplyStatus, ProfileInput, ProfileRequest
from src.domain.drive_extraction import DriveModelUnavailable
from src.domain.drive_index import TriageVerdict
from src.domain.exceptions import LLMClientError, LLMRefusalError, LLMServerError
from src.domain.llm import FinishReason, ProviderCapabilities
from src.domain.user import PerformanceTier
from src.infrastructure.drive_model_call import DriveInputRefused, classify_model_error, reply_status, with_usage
from src.infrastructure.drive_model_call import CallCounter
from src.ports.llm_port import AgentExecutionContext, LLMRequest, Message, MessagePart
from tests.unit.agents.drive_model_fakes import ScriptedLLM, prompt_builder, reply

SAFETY = reply(text="", finish=FinishReason.SAFETY)
_REQUEST = ProfileRequest(user_id="u", account_id="a", item_id="i", kind="pdf", name="f.pdf",
                          input=ProfileInput(head="text " * 60))


def _ctx(primary, fallback=None, agent_type="drive_profiler"):
    return AgentExecutionContext(
        agent_type=agent_type, provider=primary, model_name="gpt-6-luna", tier=PerformanceTier.ECO,
        capabilities=ProviderCapabilities(vision=True), provider_name="openai", fallback_provider=fallback,
        fallback_model_name="gemini-3.8-flash" if fallback else None,
        fallback_provider_name="gemini" if fallback else None, resilience_port=InMemoryProviderResilience())


def _agent(cls, agent_type, primary, fallback=None):
    return cls(AgentConfig(agent_id=f"{agent_type}_u", agent_type=agent_type),
               _ctx(ScriptedLLM(primary), ScriptedLLM(fallback) if fallback is not None else None, agent_type),
               prompt_builder())


_IMAGES = [TriageInput(index=1, data=b"\xff\xd8a", mime_type="image/jpeg", camera_prior=False),
           TriageInput(index=2, data=b"\xff\xd8b", mime_type="image/jpeg", camera_prior=False)]
_CALLS = {
    "drive_profiler": (DriveProfilerAgent, lambda a: a.profile(_REQUEST)),
    "drive_passages": (DrivePassageAgent, lambda a: a.hints([(0, "text")], title="t", what_it_is="w",
                                                            account_id="a")),
    "drive_triage": (DriveImageTriageAgent, lambda a: a.classify(_IMAGES, account_id="a")),
}


class TestFallbackSafetyBlock:
    @pytest.mark.parametrize("agent_type", sorted(_CALLS))
    async def test_a_block_served_by_the_fallback_is_a_charged_outage(self, agent_type):
        cls, call = _CALLS[agent_type]
        agent = _agent(cls, agent_type, [LLMServerError("500", http_status=500)], [SAFETY])
        with pytest.raises(DriveModelUnavailable) as caught:
            await call(agent)
        assert caught.value.charges_file is True and not isinstance(caught.value, DriveInputRefused)
        assert reply_status(caught.value) is ModelReplyStatus.FAILED

    @pytest.mark.parametrize("agent_type", sorted(_CALLS))
    async def test_the_primarys_own_block_is_still_a_refusal(self, agent_type):
        cls, call = _CALLS[agent_type]
        agent = _agent(cls, agent_type, [SAFETY], [])
        with pytest.raises(DriveInputRefused):
            await call(agent)


class _Plain(BaseAgent):
    async def can_handle(self, message):
        return True

    async def execute(self, message):
        raise NotImplementedError


async def test_base_agent_marks_a_response_the_fallback_served():
    agent = _Plain(AgentConfig(agent_id="x", agent_type="drive_profiler"))
    agent.llm = ScriptedLLM([LLMServerError("500", http_status=500)])
    agent._set_execution_context(_ctx(agent.llm, ScriptedLLM([reply({"a": 1})])))
    request = LLMRequest(model_name="gpt-6-luna", messages=[Message(role="user", parts=[MessagePart(text="hi")])])
    assert (await agent._call_llm(request)).served_by_fallback == "gemini"
    agent.llm = ScriptedLLM([reply({"a": 1})])
    agent._set_execution_context(_ctx(agent.llm, ScriptedLLM([])))
    assert (await agent._call_llm(request)).served_by_fallback is None


class TestClaudeRefusal:
    def test_the_adapter_raises_a_refusal_error(self):
        response = SimpleNamespace(stop_reason="refusal", stop_details=SimpleNamespace(category="bio",
                                                                                      explanation="no"))
        with pytest.raises(LLMRefusalError) as caught:
            ClaudeAdapter._raise_on_refusal(response)
        assert isinstance(caught.value, LLMClientError)   # every existing caller still sees a client error

    def test_the_drive_reads_it_as_a_refusal_of_the_input(self):
        mapped = with_usage(classify_model_error(LLMRefusalError("refusal (category=bio): no"), "drive_profiler"),
                            CallCounter(calls=1), 0.0)
        assert isinstance(mapped, DriveInputRefused) and reply_status(mapped) is ModelReplyStatus.REFUSED


def _photo(i):
    return {"index": i, "verdict": "photo", "description": "a hill", "objects": [], "scene": "", "visible_text": ""}


def _text(i):
    return {"index": i, "verdict": "text", "description": "", "objects": [], "scene": "", "visible_text": ""}


class TestTriage:
    async def test_twins_still_disagreeing_are_settled_as_text(self):
        twins = [TriageInput(index=i, data=b"\xff\xd8same", mime_type="image/jpeg", camera_prior=False)
                 for i in (1, 2)]
        agent = _agent(DriveImageTriageAgent, "drive_triage",
                       [reply({"images": [_photo(1), _text(2)]}), reply({"images": [_text(1), _photo(2)]})])
        out = await agent.classify(twins, account_id="a")
        assert out.results == {1: (TriageVerdict.TEXT, None), 2: (TriageVerdict.TEXT, None)} and out.missing == ()

    async def test_a_capped_correction_turn_keeps_the_answers_received(self):
        agent = _agent(DriveImageTriageAgent, "drive_triage",
                       [reply({"images": [_photo(1)]}), reply(text='{"images": [', finish=FinishReason.MAX_TOKENS)])
        out = await agent.classify(_IMAGES, account_id="a")
        assert out.output_limited and set(out.results) == {1}
