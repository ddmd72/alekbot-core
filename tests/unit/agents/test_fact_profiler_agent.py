"""FactProfilerAgent and FactProfilerRunner (FACT_PROFILE_RFC §4.1, §4.3): the user message is the fingerprint's bytes,
the request fields, the structured-output standard (schema + mime type, json.loads, one parse retry, no regex
fallback), failures sorted into invalid / refused / unavailable, the prompt assembled once and checked for its two
sections, billed to the account. Synthetic facts from the invented universe only."""
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.fact_profiler_agent import AGENT_TYPE, FactProfilerAgent, response_schema
from src.composition.fact_profiler_runner import FactProfilerRunner, profiler_model
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.exceptions import (
    BothProvidersUnavailableError, LLMAccountError, LLMClientError, LLMRateLimitError, LLMRefusalError,
    LLMServerError, LLMTimeoutError,
)
from src.domain.fact_domains import render_subject_enum_block, render_use_area_line
from src.domain.fact_profile_answer import ProfileAnswer
from src.domain.fact_profile_fingerprint import build_profiler_user_message
from src.domain.fact_profiler_reply import FactProfilerReply, ProfilerStatus
from src.domain.llm import FinishReason
from src.infrastructure.agent_config import FACT_PROFILER
from src.ports.prompt_content_store import PromptContentStore
from src.ports.quota_service import QuotaService
from tests.unit.agents.drive_model_fakes import PROMPT, ScriptedLLM, context, prompt_builder, reply

MODEL = "claude-sonnet-5-5"
TEXT = ("Alex's car insurance is with Example Mutual Insurance, policy MP-2026-118204, valid 01/04/2026 to "
        "31/03/2027, premium 412.00 EUR.")
ROME = {"hotel": "Albergo Immaginario", "booking": "RM-55120", "dates": ["2025-08-10", "2025-08-14"]}
ROME_TEXT = "Alex stayed in Rome, 10-14 Aug 2025."

ANSWER = {
    "label_words": ["car cover", "vehicle costs"],
    "time_role": "validity",
    "time_points": [{"from": "2026-04-01", "to": "2027-03-31", "precision": "day",
                     "source": "01/04/2026 to 31/03/2027"}],
    "places": [], "people": [],
    "tax_or_fine": "none", "user_identity": False, "pet": False, "state_or_law": False, "about_person": "none",
    "subject": "transport", "money_flow": True, "use_areas": [], "kinds": ["document"],
    "sensitivity": "ordinary",
}


def _agent(script, builder=None, model=MODEL, quota=None):
    llm = ScriptedLLM(script)
    agent = FactProfilerAgent(AgentConfig(agent_id=AGENT_TYPE, agent_type=AGENT_TYPE), context(llm, model),
                              builder or prompt_builder())
    agent._quota_service = quota
    return agent, llm


class TestAnswer:
    async def test_a_valid_reply_is_the_validated_answer(self):
        agent, llm = _agent([reply(ANSWER)])
        result = await agent.profile("acc-1", TEXT, None)
        assert result.status is ProfilerStatus.OK and result.calls == 1
        assert result.answer == ProfileAnswer.model_validate(ANSWER)
        assert result.model == MODEL and result.usd > 0

    async def test_the_user_message_is_the_fingerprints_bytes(self):
        agent, llm = _agent([reply(ANSWER), reply(ANSWER)])
        await agent.profile("acc-1", TEXT, None)
        await agent.profile("acc-1", ROME_TEXT, ROME)
        for request, (text, metadata) in zip(llm.requests, [(TEXT, None), (ROME_TEXT, ROME)]):
            assert len(request.messages) == 1 and request.messages[0].role == "user"
            assert [p.text for p in request.messages[0].parts] == [build_profiler_user_message(text, metadata)]

    async def test_request_fields(self):
        agent, llm = _agent([reply(ANSWER)])
        await agent.profile("acc-1", TEXT, None)
        req = llm.requests[0]
        assert req.model_name == MODEL and req.system_instruction == PROMPT
        assert req.response_mime_type == "application/json" and req.response_schema == response_schema()
        assert (req.max_tokens, req.timeout, req.thinking) == (16_000, 60, "high")
        assert req.allow_provider_storage is False
        assert not req.tools and req.turn_key is None

    async def test_not_a_delegation_agent(self):
        agent, _ = _agent([])
        message = AgentMessage.create(sender="smart", recipient=AGENT_TYPE, intent=AgentIntent.QUERY, payload={})
        assert await agent.can_handle(message) is False
        assert (await agent.execute(message)).status is AgentStatus.FAILED


class TestParseRetry:
    async def test_invalid_json_is_asked_again_with_the_reply_and_a_correction(self):
        agent, llm = _agent([reply(text="not json"), reply(ANSWER)])
        result = await agent.profile("acc-1", TEXT, None)
        assert result.status is ProfilerStatus.OK and result.calls == 2
        history = llm.requests[1].messages
        assert [m.role for m in history] == ["user", "model", "user"]
        assert history[1].parts[0].text == "not json"
        assert history[2].parts[0].text.startswith("Your previous output was not valid: ")
        assert history[2].parts[0].text.endswith("Output only the JSON object the output format describes.")
        assert history[0].parts[0].text == build_profiler_user_message(TEXT, None)

    @pytest.mark.parametrize("broken", [
        {**ANSWER, "subject": "astrology"},
        {k: v for k, v in ANSWER.items() if k != "sensitivity"},
        {**ANSWER, "kinds": ["feeling"]},
        {**ANSWER, "extra_field": 1},
    ])
    async def test_an_out_of_enum_value_or_a_missing_field_is_retried(self, broken):
        agent, llm = _agent([reply(broken), reply(ANSWER)])
        result = await agent.profile("acc-1", TEXT, None)
        assert result.status is ProfilerStatus.OK and result.calls == 2

    async def test_invalid_twice_is_invalid(self):
        agent, llm = _agent([reply(text="{"), reply(text="[]")])
        result = await agent.profile("acc-1", TEXT, None)
        assert result.status is ProfilerStatus.INVALID and result.answer is None and result.calls == 2
        assert len(llm.requests) == FACT_PROFILER.max_parse_retries + 1

    async def test_the_log_names_paths_and_types_never_values(self, caplog):
        secret = "MP-2026-118204"
        broken = {**ANSWER, "subject": secret}
        agent, _ = _agent([reply(broken), reply(broken)])
        with caplog.at_level(logging.DEBUG):
            await agent.profile("acc-1", TEXT, None)
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "invalid output" in text and "subject" in text and "value_error" in text
        assert secret not in text

    async def test_max_tokens_is_invalid_and_never_parsed(self):
        agent, llm = _agent([reply(ANSWER, finish=FinishReason.MAX_TOKENS)])
        result = await agent.profile("acc-1", TEXT, None)
        assert result.status is ProfilerStatus.INVALID and result.calls == 1


class TestFailures:
    @pytest.mark.parametrize("error, status", [
        (LLMRefusalError("declined"), ProfilerStatus.REFUSED),
        (LLMClientError("bad", http_status=400), ProfilerStatus.REFUSED),
        (LLMClientError("big", http_status=413), ProfilerStatus.REFUSED),
        (LLMClientError("odd", http_status=422), ProfilerStatus.REFUSED),
        (LLMAccountError("credit balance", http_status=400), ProfilerStatus.UNAVAILABLE),
        (LLMClientError("forbidden", http_status=403), ProfilerStatus.UNAVAILABLE),
        (LLMClientError("no status"), ProfilerStatus.UNAVAILABLE),
        (LLMRateLimitError("slow down", http_status=429), ProfilerStatus.UNAVAILABLE),
        (LLMServerError("boom", http_status=500), ProfilerStatus.UNAVAILABLE),
        (LLMTimeoutError("late"), ProfilerStatus.UNAVAILABLE),
        (asyncio.TimeoutError(), ProfilerStatus.UNAVAILABLE),
        (BothProvidersUnavailableError("claude", None, LLMServerError("x", http_status=500)),
         ProfilerStatus.UNAVAILABLE),
        (RuntimeError("surprise"), ProfilerStatus.UNAVAILABLE),
    ])
    async def test_each_error_class_has_its_status(self, error, status):
        agent, _ = _agent([error])
        result = await agent.profile("acc-1", TEXT, None)
        assert result.status is status and result.answer is None and result.calls == 1

    async def test_a_safety_stop_is_refused(self):
        agent, _ = _agent([reply(text="", finish=FinishReason.SAFETY)])
        assert (await agent.profile("acc-1", TEXT, None)).status is ProfilerStatus.REFUSED

    async def test_an_unexpected_failure_is_logged_with_its_traceback(self, caplog):
        agent, _ = _agent([RuntimeError("surprise")])
        with caplog.at_level(logging.ERROR):
            await agent.profile("acc-1", TEXT, None)
        record = next(r for r in caplog.records if "unexpected failure" in r.getMessage())
        assert record.exc_info is not None

    async def test_a_failure_on_the_retry_keeps_what_the_first_call_cost(self):
        agent, _ = _agent([reply(text="oops"), LLMServerError("boom", http_status=500)])
        result = await agent.profile("acc-1", TEXT, None)
        assert result.status is ProfilerStatus.UNAVAILABLE and result.calls == 2 and result.usd > 0


class TestPrompt:
    async def test_assembled_once_for_two_calls_with_the_registry_enum_block(self):
        builder = prompt_builder()
        agent, llm = _agent([reply(ANSWER), reply(ANSWER)], builder)
        await agent.profile("acc-1", TEXT, None)
        await agent.profile("acc-1", ROME_TEXT, ROME)
        builder.build_for_agent.assert_awaited_once_with(
            agent_type="fact_profiler", user_id=None, account_id=None, include_biographical=False,
            include_directives=False, kb_preamble=False,
            extra_static_blocks=[render_subject_enum_block() + "\n" + render_use_area_line()])

    @pytest.mark.parametrize("prompt, section", [
        ("class A extends Agent {\n    output_format {\n    x\n    }\n}", "cognitive_process"),
        ("class A extends Agent {\n    cognitive_process {\n    x\n    }\n}", "output_format"),
    ])
    async def test_a_missing_section_is_an_outage_that_evicts_the_cached_assembly(self, prompt, section, caplog):
        builder = prompt_builder(prompt)
        agent, llm = _agent([reply(ANSWER)], builder)
        with caplog.at_level(logging.ERROR):
            result = await agent.profile("acc-1", TEXT, None)
        assert result.status is ProfilerStatus.UNAVAILABLE and result.calls == 0 and llm.requests == []
        assert f"[FactProfile] token missing: {section}" in [r.getMessage() for r in caplog.records]
        builder.invalidate_agent_prompt.assert_called_once_with("fact_profiler")
        assert agent._system is None

    async def test_nothing_is_cached_after_a_missing_section_so_the_next_call_assembles_again(self):
        builder = prompt_builder("class A extends Agent {\n}")
        agent, _ = _agent([reply(ANSWER)], builder)
        await agent.profile("acc-1", TEXT, None)
        builder.build_for_agent.return_value = PROMPT
        result = await agent.profile("acc-1", TEXT, None)
        assert result.status is ProfilerStatus.OK and builder.build_for_agent.await_count == 2

    async def test_an_assembly_error_is_an_outage(self):
        agent, llm = _agent([reply(ANSWER)], prompt_builder(error=RuntimeError("firestore down")))
        result = await agent.profile("acc-1", TEXT, None)
        assert result.status is ProfilerStatus.UNAVAILABLE and result.calls == 0 and llm.requests == []


class TestBilling:
    async def test_the_account_is_billed_the_whole_cost(self):
        quota = AsyncMock(spec=QuotaService)
        agent, _ = _agent([reply(text="oops"), reply(ANSWER)], quota=quota)
        result = await agent.profile("acc-7", TEXT, None)
        quota.record_usage.assert_awaited_once()
        kwargs = quota.record_usage.await_args.kwargs
        assert kwargs["account_id"] == "acc-7" and kwargs["model"] == MODEL
        assert kwargs["cost"] > 0 and kwargs["cost"] == pytest.approx(result.usd)

    async def test_a_failed_call_that_cost_nothing_records_nothing(self):
        quota = AsyncMock(spec=QuotaService)
        agent, _ = _agent([LLMRateLimitError("slow", http_status=429)], quota=quota)
        result = await agent.profile("acc-7", TEXT, None)
        assert result.usd == 0.0
        quota.record_usage.assert_not_awaited()


# -- runner ------------------------------------------------------------------------------------------------------------

class _Builder:
    """An AgentContextBuilder double: records what it was asked to build."""

    def __init__(self, llm, error=None):
        self.llm, self.error, self.calls = llm, error, []

    def build(self, agent_type, config):
        self.calls.append((agent_type, config))
        if self.error:
            raise self.error
        return context(self.llm, "model-from-the-strategy")


class TestRunner:
    def test_profiler_model_default_and_override(self):
        assert profiler_model(None) == profiler_model("") == profiler_model("  ") == FACT_PROFILER.default_model
        assert FACT_PROFILER.default_model == "claude-sonnet-5-5"
        assert profiler_model("  claude-opus-5-5 ") == "claude-opus-5-5"

    async def test_one_agent_is_built_and_the_knob_model_reaches_the_request(self):
        llm = ScriptedLLM([reply(ANSWER), reply(ANSWER)])
        builder = _Builder(llm)
        quota, store = AsyncMock(spec=QuotaService), AsyncMock(spec=PromptContentStore)
        runner = FactProfilerRunner(builder, prompt_builder(), "claude-opus-5-5", quota, store)

        first = await runner.profile("acc-1", TEXT, None)
        second = await runner.profile("acc-2", ROME_TEXT, ROME)

        assert first.status is second.status is ProfilerStatus.OK
        assert [r.model_name for r in llm.requests] == ["claude-opus-5-5"] * 2
        assert len(builder.calls) == 1 and builder.calls[0][0] == "fact_profiler"
        agent = runner._agent
        assert agent._quota_service is quota and agent._prompt_content_store is store
        assert [c.kwargs["account_id"] for c in quota.record_usage.await_args_list] == ["acc-1", "acc-2"]

    async def test_a_build_failure_is_unavailable_and_retried_on_the_next_call(self, caplog):
        llm = ScriptedLLM([reply(ANSWER)])
        builder = _Builder(llm, error=RuntimeError("no provider"))
        runner = FactProfilerRunner(builder, prompt_builder(), MODEL)
        with caplog.at_level(logging.ERROR):
            failed = await runner.profile("acc-1", TEXT, None)
        assert failed.status is ProfilerStatus.UNAVAILABLE and failed.calls == 0 and failed.answer is None
        assert any("cannot build the profiler" in r.getMessage() for r in caplog.records)
        builder.error = None
        assert (await runner.profile("acc-1", TEXT, None)).status is ProfilerStatus.OK

    def test_the_unavailable_reply_is_a_plain_outage(self):
        assert FactProfilerReply.unavailable("m") == FactProfilerReply(ProfilerStatus.UNAVAILABLE, None, "m", 0.0, 0)
