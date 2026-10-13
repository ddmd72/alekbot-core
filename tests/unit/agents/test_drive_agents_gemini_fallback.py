"""The drive agents' Gemini fallback end to end through BaseAgent._call_llm (USER_DRIVE_INDEXING_RFC §24.2 D7 rev
3.17, §24.8; decisions/drive_model_outage_defers_model_work.md): a primary failure the base layer fails over on
(429, 5xx, timeout, network, open breaker) is re-served on gemini-3.8-flash and billed at that model's price; an
account-level 4xx is not failed over and stays an uncharged outage; only when both providers fail is it a model
outage, sorted by the primary's cause exactly as before."""
from typing import Any, Dict, List, Sequence

import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.drive_document_reader_agent import AGENT_TYPE as READER_TYPE, DriveDocumentReaderAgent
from src.agents.drive_image_triage_agent import AGENT_TYPE as TRIAGE_TYPE, DriveImageTriageAgent, TriageInput
from src.domain.agent import AgentConfig
from src.domain.billing import calculate_cost
from src.domain.drive_extraction import DriveModelUnavailable
from src.domain.drive_index import TriageVerdict
from src.domain.exceptions import (
    LLMAccountError, LLMClientError, LLMRateLimitError, LLMServerError, LLMTimeoutError, LLMUnavailableError,
)
from src.domain.llm import LLMResponse, ProviderCapabilities
from src.domain.user import PerformanceTier, UserBotConfig
from src.infrastructure.drive_model_call import DriveInputRefused, DriveModelTimedOut
from src.ports.llm_port import LLMPort, LLMRequest
from src.services.agent_context_builder import AgentContextBuilder
from src.services.provider_registry import ProviderRegistry

from tests.unit.agents.drive_model_fakes import prompt_builder, reply


class _Provider(LLMPort):
    """Answers from a script; maps tiers like the real adapter it stands for."""

    def __init__(self, tiers: Dict[PerformanceTier, str], script: Sequence[Any]) -> None:
        self._tiers = tiers
        self.script: List[Any] = list(script)
        self.requests: List[LLMRequest] = []

    async def generate_content(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def upload_file(self, path: str, mime_type: str) -> Any:
        raise NotImplementedError

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(vision=True)

    def get_model_for_tier(self, tier: PerformanceTier) -> str:
        return self._tiers[tier]

    def supports_caching(self) -> bool:
        return False


_OPENAI = {PerformanceTier.ECO: "gpt-6-luna", PerformanceTier.BALANCED: "gpt-6-luna"}
_GEMINI = {PerformanceTier.ECO: "gemini-3.5-flash-lite", PerformanceTier.BALANCED: "gemini-3.8-flash"}


def _context(agent_type: str, primary: Sequence[Any], fallback: Sequence[Any]):
    openai, gemini = _Provider(_OPENAI, primary), _Provider(_GEMINI, fallback)
    registry = ProviderRegistry()
    registry.register("openai", openai)
    registry.register("gemini", gemini)
    builder = AgentContextBuilder(registry, resilience_port=InMemoryProviderResilience())
    return builder.build(agent_type, UserBotConfig()), openai, gemini


def _reader(primary, fallback):
    ctx, openai, gemini = _context(READER_TYPE, primary, fallback)
    agent = DriveDocumentReaderAgent(AgentConfig(agent_id="drive_reader_u1", agent_type=READER_TYPE), ctx,
                                     prompt_builder())
    return agent, openai, gemini


def _triage(primary, fallback):
    ctx, openai, gemini = _context(TRIAGE_TYPE, primary, fallback)
    agent = DriveImageTriageAgent(AgentConfig(agent_id="drive_triage_u1", agent_type=TRIAGE_TYPE), ctx,
                                  prompt_builder())
    return agent, openai, gemini


async def _read(agent):
    return await agent.read(data=b"%PDF-1.4", mime_type="application/pdf", file_name="w.pdf", first_window=False,
                            account_id="acc-1")


_IMAGES = [TriageInput(index=1, data=b"\xff\xd8\xff", mime_type="image/jpeg", camera_prior=True),
           TriageInput(index=2, data=b"\x89PNG", mime_type="image/png", camera_prior=False)]
_ANSWER = {"images": [{"index": 1, "verdict": "photo", "caption": "a beach"},
                      {"index": 2, "verdict": "text", "caption": ""}]}


@pytest.mark.parametrize("cause", [LLMUnavailableError("503", http_status=503),
                                   LLMRateLimitError("429", http_status=429),
                                   LLMServerError("500", http_status=500),
                                   LLMTimeoutError("timeout")], ids=["503", "429", "500", "timeout"])
async def test_a_reader_failure_the_base_layer_fails_over_is_read_by_gemini_flash(cause):
    agent, openai, gemini = _reader([cause], [reply({"text": "Hello", "brief": None})])
    out = await _read(agent)
    assert out.text == "Hello"
    assert [r.model_name for r in openai.requests] == ["gpt-6-luna"]
    assert [r.model_name for r in gemini.requests] == ["gemini-3.8-flash"]
    assert gemini.requests[0].messages[0].parts[0].file_data["mime_type"] == "application/pdf"


async def test_the_failover_is_billed_at_gemini_flash_prices():
    agent, _, _ = _reader([LLMUnavailableError("503", http_status=503)], [reply({"text": "x", "brief": None})])
    out = await _read(agent)
    assert out.cost == pytest.approx(calculate_cost("gemini-3.8-flash", 1000, 200)) and out.cost > 0
    assert out.calls == 1


async def test_a_triage_batch_fails_over_to_gemini_flash_with_its_images():
    agent, openai, gemini = _triage([LLMUnavailableError("503", http_status=503)], [reply(_ANSWER)])
    out = await agent.classify(_IMAGES, account_id="acc-1")
    assert out.verdicts == {1: (TriageVerdict.PHOTO, "a beach"), 2: (TriageVerdict.TEXT, "")}
    assert gemini.requests[0].model_name == "gemini-3.8-flash"
    files = [p.file_data["mime_type"] for p in gemini.requests[0].messages[0].parts if p.file_data]
    assert files == ["image/jpeg", "image/png"]


async def test_an_account_error_is_not_failed_over_and_stays_an_uncharged_outage():
    agent, _, gemini = _reader([LLMAccountError("credit balance too low", http_status=400)], [])
    with pytest.raises(DriveModelUnavailable) as caught:
        await _read(agent)
    assert caught.value.charges_file is False
    assert gemini.requests == []


async def test_a_model_or_key_4xx_on_the_primary_is_not_failed_over():
    agent, _, gemini = _reader([LLMClientError("model not found", http_status=404)], [])
    with pytest.raises(DriveModelUnavailable):
        await _read(agent)
    assert gemini.requests == []


async def test_both_down_with_a_5xx_is_an_outage_charged_by_the_primarys_cause():
    agent, _, _ = _reader([LLMServerError("500", http_status=500)], [LLMUnavailableError("503", http_status=503)])
    with pytest.raises(DriveModelUnavailable) as caught:
        await _read(agent)
    assert caught.value.charges_file is True


async def test_both_rate_limited_is_an_uncharged_outage():
    agent, _, _ = _reader([LLMRateLimitError("429", http_status=429)], [LLMRateLimitError("429", http_status=429)])
    with pytest.raises(DriveModelUnavailable) as caught:
        await _read(agent)
    assert caught.value.charges_file is False


async def test_both_timed_out_is_a_timeout_for_the_window_halving():
    agent, _, _ = _reader([LLMTimeoutError("t")], [LLMTimeoutError("t")])
    with pytest.raises(DriveModelTimedOut):
        await _read(agent)


async def test_a_triage_outage_on_both_is_a_model_outage():
    agent, _, _ = _triage([LLMUnavailableError("503", http_status=503)], [LLMServerError("500", http_status=500)])
    with pytest.raises(DriveModelUnavailable):
        await agent.classify(_IMAGES, account_id="acc-1")


async def test_a_gemini_refusal_on_failover_is_read_as_a_refusal_of_the_input():
    """Recorded residual: a 400 the fallback raises is not wrapped by the base layer, so it is sorted like the
    primary's own 400 (a refusal of this input). The schema and inline-bytes wire tests are what keep a systematic
    Gemini 400 from happening; this test pins the behaviour so a change to it is a decision."""
    agent, _, _ = _reader([LLMUnavailableError("503", http_status=503)], [LLMClientError("bad", http_status=400)])
    with pytest.raises(DriveInputRefused):
        await _read(agent)
