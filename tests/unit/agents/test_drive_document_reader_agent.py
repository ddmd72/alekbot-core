"""DriveDocumentReaderAgent (USER_DRIVE_INDEXING_RFC §24.2, §24.4, §24.8): one window in, verbatim text and — first
window only — the brief out; the structured-output standard (schema + mime type, json.loads, one parse retry, no
regex fallback); the output-limit signal; provider failures sorted into per-input and outage; billed per call."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.agents.drive_document_reader_agent import AGENT_TYPE, DOC_TYPES, DriveDocumentReaderAgent
from src.infrastructure.drive_model_call import DriveModelCallFailed
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.drive_extraction import DriveModelUnavailable
from src.domain.exceptions import (
    BothProvidersUnavailableError, LLMClientError, LLMRateLimitError,
)
from src.domain.llm import FinishReason
from src.infrastructure.agent_config import DRIVE_READER

from tests.unit.agents.drive_model_fakes import PROMPT, ScriptedLLM, context, prompt_builder, reply

BRIEF = {"doc_type": "invoice", "description": "Рахунок за електроенергію за вересень",
         "doc_date": "2026-09-30", "parties": ["ТОВ Енерго", "Alex Doe"], "amount": {"value": "1 250,00",
                                                                                   "currency": "UAH"}}


def _agent(script, builder=None, thinking=None):
    llm = ScriptedLLM(script)
    agent = DriveDocumentReaderAgent(AgentConfig(agent_id="drive_reader_u1", agent_type=AGENT_TYPE),
                                     context(llm), builder or prompt_builder(), thinking=thinking)
    return agent, llm


async def _read(agent, first=True, account="acc-1"):
    return await agent.read(data=b"%PDF-1.4", mime_type="application/pdf", file_name="window-001.pdf",
                            first_window=first, account_id=account)


class TestRequest:
    async def test_one_file_part_and_the_task_with_schema_and_mime_type(self):
        agent, llm = _agent([reply({"text": "Hello", "brief": None})])
        await _read(agent)
        req = llm.requests[0]
        assert req.system_instruction == PROMPT
        assert req.response_mime_type == "application/json"
        assert req.response_schema["required"] == ["text", "brief"]
        assert req.max_tokens == DRIVE_READER.max_tokens and req.timeout == DRIVE_READER.call_timeout_s
        assert req.thinking == "low" and req.tools is None
        assert req.allow_provider_storage is False   # no verbatim scan kept at the provider (§24.2)
        file_part, task = req.messages[0].parts
        assert file_part.file_data == {"base64": "JVBERi0xLjQ=", "mime_type": "application/pdf",
                                       "name": "window-001.pdf"}
        assert "fill `brief`" in task.text

    async def test_later_window_asks_for_no_brief(self):
        agent, llm = _agent([reply({"text": "x", "brief": None})])
        await _read(agent, first=False)
        assert "set `brief` to null" in llm.requests[0].messages[0].parts[1].text

    async def test_user_thinking_override_wins(self):
        agent, llm = _agent([reply({"text": "x", "brief": None})], thinking="medium")
        await _read(agent)
        assert llm.requests[0].thinking == "medium"

    async def test_prompt_built_without_biography_directives_or_user_overrides(self):
        builder = prompt_builder()
        agent, _ = _agent([reply({"text": "x", "brief": None}), reply({"text": "y", "brief": None})], builder)
        await _read(agent)
        await _read(agent, first=False)
        builder.build_for_agent.assert_awaited_once_with(agent_type="drive_reader", user_id=None, account_id=None,
                                                         include_biographical=False, include_directives=False)

    def test_doc_types_match_the_rfc(self):
        assert DOC_TYPES == ("invoice", "receipt", "contract", "statement", "letter", "ticket", "certificate",
                             "form", "other")


class TestParse:
    async def test_text_verbatim_and_brief_on_the_first_window(self):
        agent, _ = _agent([reply({"text": "  Рахунок № 17\n| a | b |\n", "brief": BRIEF})])
        out = await _read(agent)
        assert out.text == "Рахунок № 17\n| a | b |"
        assert out.brief.doc_type == "invoice" and out.brief.description == BRIEF["description"]
        assert out.brief.doc_date == datetime(2026, 9, 30, tzinfo=timezone.utc)
        assert out.brief.parties == ("ТОВ Енерго", "Alex Doe") and out.brief.amount == "1 250,00 UAH"
        assert out.output_limited is False

    async def test_a_brief_on_a_later_window_is_ignored(self):
        agent, _ = _agent([reply({"text": "x", "brief": BRIEF})])
        assert (await _read(agent, first=False)).brief is None

    async def test_null_brief_null_date_and_amount(self):
        agent, _ = _agent([reply({"text": "x", "brief": {**BRIEF, "doc_date": None, "amount": None}})])
        out = await _read(agent)
        assert out.brief.doc_date is None and out.brief.amount == ""

    async def test_bounds_on_card_fields(self):
        brief = {**BRIEF, "doc_type": "Bank Letter", "description": "a  b\n" * 200,
                 "parties": [f"P{i}" for i in range(9)] + [""], "doc_date": "30.09.2026"}
        agent, _ = _agent([reply({"text": "x", "brief": brief})])
        out = await _read(agent)
        assert out.brief.doc_type == "other"
        assert len(out.brief.description) == 300 and "\n" not in out.brief.description
        assert out.brief.parties == ("P0", "P1", "P2", "P3", "P4")
        assert out.brief.doc_date is None

    async def test_invalid_json_is_retried_once_with_a_correction(self):
        agent, llm = _agent([reply(text='```json\n{"text": "x"}```'), reply({"text": "fixed", "brief": None})])
        assert (await _read(agent)).text == "fixed"
        retry = llm.requests[1].messages
        assert [m.role for m in retry] == ["user", "model", "user"]
        assert retry[1].parts[0].text.startswith("```json") and "not valid" in retry[2].parts[0].text

    @pytest.mark.parametrize("bad", ['{"brief": null}', '["x"]', '{"text": 3, "brief": null}',
                                     '{"text": "x", "brief": "invoice"}',
                                     '{"text": "x", "brief": {"parties": "A"}}',
                                     '{"text": "x", "brief": {"amount": "10 EUR"}}'])
    async def test_wrong_shapes_fail_after_the_retry(self, bad):
        agent, llm = _agent([reply(text=bad), reply(text=bad)])
        with pytest.raises(DriveModelCallFailed):
            await _read(agent)
        assert len(llm.requests) == 2


class TestStops:
    async def test_output_limit_is_reported_not_parsed(self):
        agent, llm = _agent([reply(text='{"text": "cut mid', finish=FinishReason.MAX_TOKENS)])
        out = await _read(agent)
        assert out.output_limited is True and out.text == "" and len(llm.requests) == 1

    async def test_safety_stop_is_a_per_input_failure(self):
        agent, _ = _agent([reply(text="", finish=FinishReason.SAFETY)])
        with pytest.raises(DriveModelCallFailed):
            await _read(agent)


class TestFailures:
    @pytest.mark.parametrize("status", [400, 413, 422, None])
    async def test_refusal_of_the_input_is_per_input(self, status):
        agent, _ = _agent([LLMClientError("bad pdf", http_status=status)])
        with pytest.raises(DriveModelCallFailed):
            await _read(agent)

    @pytest.mark.parametrize("exc", [
        LLMClientError("invalid x-api-key", http_status=401), LLMClientError("model not found", http_status=404),
        BothProvidersUnavailableError("claude", None, LLMRateLimitError("429", http_status=429)),
        RuntimeError("bug"),
    ])
    async def test_everything_else_is_a_model_outage(self, exc):
        agent, _ = _agent([exc])
        with pytest.raises(DriveModelUnavailable):
            await _read(agent)

    async def test_rate_limit_without_fallback_is_an_outage(self):
        agent, llm = _agent([LLMRateLimitError("429", http_status=429)])
        with pytest.raises(DriveModelUnavailable):
            await _read(agent)
        assert len(llm.requests) == 1

    async def test_prompt_assembly_failure_is_an_outage_and_no_call_is_made(self):
        agent, llm = _agent([], prompt_builder(error=KeyError("drive_reader_agent_v1")))
        with pytest.raises(DriveModelUnavailable):
            await _read(agent)
        assert llm.requests == []

    @pytest.mark.parametrize("prompt", ["", "class X extends Agent {\n\n}",
                                        "class X {\n\n    cognitive_process {\n\n    a\n\n    }\n\n}"])
    async def test_prompt_without_both_sections_is_an_outage(self, prompt):
        agent, llm = _agent([], prompt_builder(prompt=prompt))
        with pytest.raises(DriveModelUnavailable):
            await _read(agent)
        assert llm.requests == []


class TestBilling:
    async def test_each_call_is_billed_to_the_account_and_costed(self):
        agent, _ = _agent([reply({"text": "x", "brief": None}, prompt_tokens=10_000, completion_tokens=2_000)])
        quota = AsyncMock()
        agent._quota_service = quota
        out = await _read(agent, account="acc-9")
        # claude-haiku-5-5: $0.10 / $0.50 per million (§24.9)
        assert out.cost == pytest.approx(0.002)
        quota.record_usage.assert_awaited_once()
        kwargs = quota.record_usage.await_args.kwargs
        assert kwargs["account_id"] == "acc-9" and kwargs["tokens"] == 12_000
        assert kwargs["cost"] == pytest.approx(0.002)

    async def test_a_failed_call_still_bills_what_it_used(self):
        agent, _ = _agent([reply(text="nope"), reply(text="nope")])
        quota = AsyncMock()
        agent._quota_service = quota
        with pytest.raises(DriveModelCallFailed):
            await _read(agent)
        assert quota.record_usage.await_args.kwargs["tokens"] == 2400


class TestNotADelegationAgent:
    async def test_cannot_handle_and_execute_refuses(self):
        agent, _ = _agent([])
        msg = AgentMessage.create(sender="t", recipient=agent.agent_id, intent=AgentIntent.DELEGATE, payload={})
        assert await agent.can_handle(msg) is False
        assert (await agent.execute(msg)).status == AgentStatus.FAILED
