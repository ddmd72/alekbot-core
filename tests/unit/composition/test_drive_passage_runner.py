"""DrivePassageRunner (USER_DRIVE_INDEXING_RFC §25.4.2, §25.4.4, §25.4.6): the passages agent as a PassageHintPort — an
outcome per chunk the calls settled, the batch's deciding status, halving of a batch stopped at the cap or refused,
an outage that cuts the batch, the model, the cost and the fallback's share."""
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.composition.drive_passage_runner import DrivePassageRunner
from src.domain.billing import calculate_cost
from src.domain.drive_enrichment import HintItem
from src.domain.drive_enrichment_calls import HintReply, HintRequest, ModelReplyStatus as S
from src.domain.exceptions import LLMClientError, LLMRateLimitError, LLMServerError
from src.domain.llm import FinishReason, ProviderCapabilities
from src.domain.user import PerformanceTier, UserBotConfig
from src.ports.llm_port import AgentExecutionContext
from src.ports.passage_hint_port import PassageHintPort
from tests.unit.agents.drive_model_fakes import RoutedLLM, prompt_builder, reply

CHUNKS = [(4, "first passage about Roma"), (5, "second passage about Lisboa")]


def _labels(request):
    return [int(n) for n in re.findall(r'<passage n="(\d+)">', request.messages[0].parts[0].text)]


def _texts(request):
    return re.findall(r'<passage n="\d+">\n(.*?)\n</passage>', request.messages[0].parts[0].text, re.S)


def _answer_all(request):
    return reply({"passages": [{"index": i, "items": [{"type": "keyword", "text": f"k{i}", "evidence": "about"}]}
                               for i in _labels(request)]})


def _ctx(llm, fallback=None):
    return AgentExecutionContext(
        agent_type="drive_passages", provider=llm, model_name="gpt-6-luna", tier=PerformanceTier.ECO,
        capabilities=ProviderCapabilities(), provider_name="openai", fallback_provider=fallback,
        fallback_model_name="gemini-3.8-flash" if fallback else None,
        fallback_provider_name="gemini" if fallback else None, resilience_port=InMemoryProviderResilience())


def _runner(handler, fallback_handler=None, quota=None):
    llm = RoutedLLM(handler)
    fallback = RoutedLLM(fallback_handler) if fallback_handler else None
    users = AsyncMock()
    users.get_user.return_value = SimpleNamespace(config=UserBotConfig())
    contexts = MagicMock()
    contexts.build.return_value = _ctx(llm, fallback)
    return DrivePassageRunner(contexts, users, prompt_builder(), quota_service=quota), llm, contexts


def _request(chunks=CHUNKS):
    return HintRequest(user_id="user-1", account_id="acc-1", item_id="item-1", chunks=chunks, title="Trip",
                       what_it_is="Travel notes")


def test_it_is_the_port():
    assert issubclass(DrivePassageRunner, PassageHintPort)


async def test_items_and_outcomes_by_chunk_index():
    runner, llm, contexts = _runner(_answer_all)
    out = await runner.hints(_request())
    assert out.status is S.OK and out.outcome_by_index == {4: S.OK, 5: S.OK}
    assert out.items_by_index == {4: (HintItem("k1", "keyword", "about"),), 5: (HintItem("k2", "keyword", "about"),)}
    assert out.model == "gpt-6-luna" and out.usd == pytest.approx(calculate_cost("gpt-6-luna", 1000, 200))
    assert contexts.build.call_args.args[0] == "drive_passages" and len(llm.requests) == 1


async def test_billed_to_the_account():
    quota = AsyncMock()
    runner, _, _ = _runner(_answer_all, quota=quota)
    await runner.hints(_request())
    assert quota.record_usage.await_args.kwargs["account_id"] == "acc-1"


async def test_a_batch_stopped_at_the_cap_is_split():
    def handler(request):
        if len(_labels(request)) > 1:
            return reply(text='{"passages": [', finish=FinishReason.MAX_TOKENS)
        return _answer_all(request)

    runner, llm, _ = _runner(handler)
    out = await runner.hints(_request())
    assert out.status is S.OK and out.outcome_by_index == {4: S.OK, 5: S.OK} and len(llm.requests) == 3
    assert out.usd == pytest.approx(3 * calculate_cost("gpt-6-luna", 1000, 200))


async def test_a_single_chunk_still_stopped_gets_no_hints():
    def handler(request):
        if "Lisboa" in _texts(request)[-1]:
            return reply(text='{"passages": [', finish=FinishReason.MAX_TOKENS)
        return _answer_all(request)

    runner, _, _ = _runner(handler)
    out = await runner.hints(_request())
    assert out.outcome_by_index == {4: S.OK, 5: S.STOPPED_AT_CAP} and out.status is S.STOPPED_AT_CAP
    assert set(out.items_by_index) == {4}


async def test_a_refused_batch_is_halved_and_a_refused_chunk_alone_gets_no_hints():
    def handler(request):
        if any("Lisboa" in t for t in _texts(request)):
            return LLMClientError("refused", http_status=400)
        return _answer_all(request)

    runner, llm, _ = _runner(handler)
    out = await runner.hints(_request())
    assert out.outcome_by_index == {4: S.OK, 5: S.REFUSED}
    assert out.status is S.OK          # refused chunks alone leave the batch ok: the file is not held
    assert len(llm.requests) == 3


async def test_an_outage_cuts_the_batch_and_leaves_the_rest_unattempted():
    def handler(request):
        if len(_labels(request)) > 1:
            return reply(text="{", finish=FinishReason.MAX_TOKENS)
        if "Roma" in _texts(request)[0]:
            return _answer_all(request)
        return LLMRateLimitError("429", http_status=429)

    runner, _, _ = _runner(handler)
    out = await runner.hints(_request())
    assert out.status is S.UNAVAILABLE and out.outcome_by_index == {4: S.OK} and set(out.items_by_index) == {4}


async def test_a_failure_is_charged_and_stops_the_batch():
    runner, llm, _ = _runner(lambda r: LLMServerError("500", http_status=500))
    out = await runner.hints(_request())
    assert out.status is S.FAILED and out.outcome_by_index == {4: S.FAILED, 5: S.FAILED} and len(llm.requests) == 1


async def test_invalid_output_after_the_retry():
    runner, _, _ = _runner(lambda r: reply(text="nope"))
    out = await runner.hints(_request())
    assert out.status is S.INVALID and out.outcome_by_index == {4: S.INVALID, 5: S.INVALID}


async def test_a_chunk_left_out_after_the_correction_is_invalid():
    def handler(request):
        return reply({"passages": [{"index": 1, "items": []}]})

    runner, _, _ = _runner(handler)
    out = await runner.hints(_request())
    assert out.outcome_by_index == {4: S.OK, 5: S.INVALID} and out.status is S.INVALID
    assert out.items_by_index == {4: ()}


async def test_the_fallback_serves_with_its_share():
    runner, _, _ = _runner(lambda r: LLMServerError("500", http_status=500), fallback_handler=_answer_all)
    out = await runner.hints(_request())
    assert out.status is S.OK and out.model == "gemini-3.8-flash"
    assert out.fallback_usd == pytest.approx(calculate_cost("gemini-3.8-flash", 1000, 200)) == out.usd


async def test_a_refusal_by_the_fallback_is_failed_not_refused():
    runner, _, _ = _runner(lambda r: LLMServerError("500", http_status=500),
                           fallback_handler=lambda r: LLMClientError("no", http_status=400))
    out = await runner.hints(_request())
    assert out.status is S.FAILED and set(out.outcome_by_index.values()) == {S.FAILED}


async def test_no_user_is_unavailable_with_no_outcomes():
    runner, _, _ = _runner(_answer_all)
    runner._user_repo.get_user.return_value = None
    out = await runner.hints(_request())
    assert isinstance(out, HintReply) and out.status is S.UNAVAILABLE and out.outcome_by_index == {}


async def test_no_chunks():
    runner, llm, _ = _runner(_answer_all)
    out = await runner.hints(_request([]))
    assert out.status is S.OK and llm.requests == []
