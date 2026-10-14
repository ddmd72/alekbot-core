"""DrivePassageAgent (USER_DRIVE_INDEXING_RFC §25.4.2, §25.4.4): a few chunks under opaque indexes plus the profile's
title and what_it_is in, keywords and questions with evidence quotes per chunk out; mapping back by index with
unknown, repeated and missing indexes handled deterministically; one correction turn; the output-limit signal;
billed per call. Items come back ungrounded (the caller runs drive_hints.ground)."""
import pytest

from src.agents.drive_passage_agent import AGENT_TYPE, DrivePassageAgent, _RESPONSE_SCHEMA
from src.domain.agent import AgentConfig
from src.domain.drive_enrichment import UNGROUNDED, HintItem
from src.domain.drive_extraction import DriveModelUnavailable
from src.domain.exceptions import LLMClientError, LLMServerError
from src.domain.llm import FinishReason
from src.infrastructure.agent_config import DRIVE_PASSAGES
from src.infrastructure.drive_model_call import DriveInputRefused, DriveOutputInvalid
from tests.unit.agents.drive_model_fakes import ScriptedLLM, context, prompt_builder, reply

CHUNKS = [(7, "Confirmamos su reserva en Roma, 3 noches."), (8, "Importe total: 1.250,00 €.")]


def _answer(*passages):
    return reply({"passages": [{"index": i, "items": items} for i, items in passages]})


def _kw(text, evidence):
    return {"type": "keyword", "text": text, "evidence": evidence}


def _agent(script, builder=None):
    llm = ScriptedLLM(script)
    agent = DrivePassageAgent(AgentConfig(agent_id="drive_passages_u1", agent_type=AGENT_TYPE), context(llm),
                              builder or prompt_builder())
    return agent, llm


async def _hints(agent, chunks=CHUNKS):
    return await agent.hints(chunks, title="Reserva", what_it_is="A hotel booking", account_id="acc-1")


class TestRequest:
    async def test_opaque_indexes_title_and_limits(self):
        agent, llm = _agent([_answer((1, [_kw("Rome", "reserva en Roma")]), (2, []))])
        await _hints(agent)
        req = llm.requests[0]
        text = req.messages[0].parts[0].text
        assert '<passage n="1">\nConfirmamos su reserva en Roma, 3 noches.\n</passage>' in text
        assert '<passage n="2">' in text and 'n="7"' not in text
        assert "Reserva — A hotel booking" in text and "at most 12 keywords and 3 questions" in text
        assert req.response_schema is _RESPONSE_SCHEMA and req.response_mime_type == "application/json"
        assert (req.max_tokens, req.timeout, req.thinking) == (DRIVE_PASSAGES.max_tokens,
                                                               DRIVE_PASSAGES.call_timeout_s, "low")
        assert req.allow_provider_storage is False

    async def test_no_chunks_no_call(self):
        agent, llm = _agent([])
        out = await _hints(agent, [])
        assert out.items_by_index == {} and llm.requests == []


class TestMapping:
    async def test_items_by_chunk_index_ungrounded(self):
        agent, _ = _agent([_answer((2, [_kw("total", "Importe total")]),
                                   (1, [_kw("Rome", "reserva en Roma"),
                                        {"type": "question", "text": "Where?", "evidence": "en Roma"}]))])
        out = await _hints(agent)
        assert out.items_by_index[7] == (HintItem("Rome", "keyword", "reserva en Roma"),
                                         HintItem("Where?", "question", "en Roma"))
        assert out.items_by_index[8] == (HintItem("total", "keyword", "Importe total"),)
        assert out.items_by_index[7][0].evidence_start == UNGROUNDED and out.missing == ()

    async def test_unknown_index_ignored_and_first_answer_wins(self):
        agent, llm = _agent([_answer((1, [_kw("a", "b")]), (1, [_kw("c", "d")]), (9, [_kw("e", "f")]),
                                     (2, []))])
        out = await _hints(agent)
        assert out.items_by_index == {7: (HintItem("a", "keyword", "b"),), 8: ()} and len(llm.requests) == 1

    async def test_missing_indexes_get_one_correction_turn(self):
        agent, llm = _agent([_answer((1, [])), _answer((2, [_kw("x", "y")]))])
        out = await _hints(agent)
        assert set(out.items_by_index) == {7, 8} and out.calls == 2
        assert "left out passages [2]" in llm.requests[1].messages[-1].parts[0].text

    async def test_still_missing_is_reported(self):
        agent, _ = _agent([_answer((1, [])), _answer((1, []))])
        out = await _hints(agent)
        assert out.missing == (8,) and set(out.items_by_index) == {7}

    @pytest.mark.parametrize("bad", ['{"passages": "x"}', '[]', '{"passages": [{"index": "1", "items": []}]}',
                                     '{"passages": [{"index": 1, "items": [{"type": 1}]}]}'])
    async def test_nothing_usable_after_the_retry_is_invalid(self, bad):
        agent, _ = _agent([reply(text=bad), reply(text=bad)])
        with pytest.raises(DriveOutputInvalid):
            await _hints(agent)

    async def test_the_output_limit_is_reported(self):
        agent, _ = _agent([reply(text='{"passages": [', finish=FinishReason.MAX_TOKENS)])
        out = await _hints(agent)
        assert out.output_limited and out.items_by_index == {}


class TestFailures:
    async def test_refusal(self):
        agent, _ = _agent([LLMClientError("no", http_status=422)])
        with pytest.raises(DriveInputRefused):
            await _hints(agent)

    async def test_outage(self):
        agent, _ = _agent([LLMServerError("500", http_status=500)])
        with pytest.raises(DriveModelUnavailable):
            await _hints(agent)

    async def test_safety_stop_is_a_refusal(self):
        agent, _ = _agent([reply(text="", finish=FinishReason.SAFETY)])
        with pytest.raises(DriveInputRefused):
            await _hints(agent)


def test_schema_types_are_keyword_and_question():
    item = _RESPONSE_SCHEMA["properties"]["passages"]["items"]["properties"]["items"]["items"]
    assert item["properties"]["type"]["enum"] == ["keyword", "question"]
    assert set(item["required"]) == {"type", "text", "evidence"} and "null" not in str(_RESPONSE_SCHEMA)
