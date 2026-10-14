"""DriveProfilerRunner (USER_DRIVE_INDEXING_RFC §25.4.1, §25.4.4, §25.4.6): the profiler as a DocumentProfilerPort —
built from the user's configuration and billed to the account; every outcome a ModelReplyStatus, never an exception;
a reply stopped at the cap asked once more with halved list caps; the kinds without key values; the model that served
and the fallback's share in dollars."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.composition.drive_profiler_runner import DriveProfilerRunner
from src.domain.billing import calculate_cost
from src.domain.drive_enrichment_calls import ModelReplyStatus as S, ProfileInput, ProfileReply, ProfileRequest
from src.domain.exceptions import (
    LLMAccountError, LLMClientError, LLMFallbackClientError, LLMRateLimitError, LLMServerError,
)
from src.domain.llm import FinishReason
from src.domain.user import PerformanceTier, UserBotConfig
from src.ports.document_profiler_port import DocumentProfilerPort
from src.ports.llm_port import AgentExecutionContext
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.domain.llm import ProviderCapabilities
from tests.unit.agents.drive_model_fakes import ScriptedLLM, prompt_builder, reply
from tests.unit.agents.test_drive_profiler_agent import ANSWER


def _request(kind="pdf"):
    return ProfileRequest(user_id="user-1", account_id="acc-1", item_id="item-1", kind=kind, name="f.pdf",
                          input=ProfileInput(head="text " * 50))


def _ctx(primary, fallback=None):
    return AgentExecutionContext(
        agent_type="drive_profiler", provider=primary, model_name="gpt-6-luna", tier=PerformanceTier.ECO,
        capabilities=ProviderCapabilities(), provider_name="openai", fallback_provider=fallback,
        fallback_model_name="gemini-3.8-flash" if fallback else None,
        fallback_provider_name="gemini" if fallback else None, resilience_port=InMemoryProviderResilience())


def _runner(script, fallback_script=None, user_missing=False, build_error=None, quota=None, builder=None):
    llm = ScriptedLLM(script)
    fallback = ScriptedLLM(fallback_script) if fallback_script is not None else None
    users = AsyncMock()
    users.get_user.return_value = None if user_missing else SimpleNamespace(config=UserBotConfig())
    contexts = MagicMock()
    contexts.build.side_effect = build_error
    contexts.build.return_value = _ctx(llm, fallback)
    return DriveProfilerRunner(contexts, users, builder or prompt_builder(), quota_service=quota), llm, contexts


def test_it_is_the_port():
    assert issubclass(DriveProfilerRunner, DocumentProfilerPort)


async def test_ok_with_the_model_and_cost():
    runner, llm, contexts = _runner([reply(ANSWER)])
    out = await runner.profile(_request())
    assert out.status is S.OK and out.profile.doc_type == "invoice"
    assert out.model == "gpt-6-luna" and out.usd == pytest.approx(calculate_cost("gpt-6-luna", 1000, 200))
    assert out.fallback_usd == 0.0
    assert contexts.build.call_args.args[0] == "drive_profiler"


async def test_billed_to_the_account():
    quota = AsyncMock()
    runner, _, _ = _runner([reply(ANSWER)], quota=quota)
    await runner.profile(_request())
    assert quota.record_usage.await_args.kwargs["account_id"] == "acc-1"


@pytest.mark.parametrize("kind", ["xlsx", "csv", "json", "epub", "folder"])
async def test_kinds_without_key_values(kind):
    runner, llm, _ = _runner([reply(ANSWER)])
    out = await runner.profile(_request(kind))
    assert out.profile.key_values == ()
    assert "has no key values" in llm.requests[0].messages[0].parts[0].text


async def test_stopped_at_the_cap_is_asked_once_more_with_halved_caps():
    capped = reply(text='{"title": "', finish=FinishReason.MAX_TOKENS)
    runner, llm, _ = _runner([capped, reply(ANSWER)])
    out = await runner.profile(_request())
    assert out.status is S.OK and len(llm.requests) == 2
    assert "at most 12 entities" in llm.requests[0].messages[0].parts[0].text
    assert "at most 6 entities" in llm.requests[1].messages[0].parts[0].text
    assert out.usd == pytest.approx(2 * calculate_cost("gpt-6-luna", 1000, 200))


async def test_still_stopped_after_halving():
    capped = reply(text='{"title": "', finish=FinishReason.MAX_TOKENS)
    runner, llm, _ = _runner([capped, capped])
    out = await runner.profile(_request())
    assert (out.status, out.profile, len(llm.requests)) == (S.STOPPED_AT_CAP, None, 2)
    assert out.usd > 0


@pytest.mark.parametrize("script, status", [
    ([LLMClientError("refused", http_status=400)], S.REFUSED),
    ([LLMClientError("too large", http_status=413)], S.REFUSED),
    ([reply(text="nope"), reply(text="nope")], S.INVALID),
    ([reply(text="", finish=FinishReason.SAFETY)], S.REFUSED),
    ([LLMRateLimitError("429", http_status=429)], S.UNAVAILABLE),
    ([LLMAccountError("billing", http_status=400)], S.UNAVAILABLE),
    ([LLMServerError("500", http_status=500)], S.FAILED),
])
async def test_outcomes(script, status):
    runner, _, _ = _runner(script)
    out = await runner.profile(_request())
    assert out.status is status and out.profile is None


async def test_the_fallback_serves_and_its_share_is_in_dollars():
    runner, _, _ = _runner([LLMServerError("500", http_status=500)], [reply(ANSWER)])
    out = await runner.profile(_request())
    assert out.status is S.OK and out.model == "gemini-3.8-flash"
    assert out.usd == out.fallback_usd == pytest.approx(calculate_cost("gemini-3.8-flash", 1000, 200))


async def test_a_refusal_by_the_fallback_is_a_charged_attempt_not_a_refusal():
    runner, _, _ = _runner([LLMServerError("500", http_status=500)], [LLMClientError("no", http_status=400)])
    out = await runner.profile(_request())
    assert out.status is S.FAILED


async def test_a_missing_prompt_is_unavailable():
    runner, llm, _ = _runner([], builder=prompt_builder(prompt="class X {\n}"))
    out = await runner.profile(_request())
    assert out.status is S.UNAVAILABLE and llm.requests == [] and out.usd == 0.0


async def test_no_user_or_no_provider_is_unavailable():
    runner, _, _ = _runner([], user_missing=True)
    assert (await runner.profile(_request())).status is S.UNAVAILABLE
    runner, _, _ = _runner([], build_error=ValueError("no key for claude"))
    assert (await runner.profile(_request())).status is S.UNAVAILABLE


async def test_never_raises_on_an_unexpected_error():
    runner, _, _ = _runner([KeyError("bug")])
    out = await runner.profile(_request())
    assert isinstance(out, ProfileReply) and out.status is S.FAILED
