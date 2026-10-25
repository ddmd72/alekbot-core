"""DriveProfilerAgent (USER_DRIVE_INDEXING_RFC §25.4.1, §25.4.4, §25.4.6): the profiler's input as data, the caps and
the previous labels in the task, the structured-output standard (schema + mime type, json.loads, one parse retry, no
regex fallback), the output-limit signal, failures sorted per input or outage, billed per call with the fallback's
share. Synthetic inputs only."""
from datetime import date
from unittest.mock import AsyncMock

import pytest

from src.agents.drive_profiler_agent import (
    AGENT_TYPE, FULL_CAPS, ListCaps, DriveProfilerAgent, _RESPONSE_SCHEMA,
)
from src.domain.agent import AgentConfig
from src.domain.billing import calculate_cost
from src.domain.drive_enrichment import DriveProfile, KeyValue, ProfileDate, ProfileEntity
from src.domain.drive_enrichment_calls import ProfileInput, ProfileLabels, ProfileRequest
from src.domain.drive_extraction import DriveModelUnavailable
from src.domain.drive_labels import DATE_LABELS, DOC_TYPES, ENTITY_TYPES, LIFE_DOMAINS, VALUE_LABELS
from src.domain.exceptions import LLMClientError, LLMServerError
from src.domain.llm import FinishReason
from src.infrastructure.agent_config import DRIVE_PROFILER
from src.infrastructure.drive_model_call import DriveInputRefused, DriveOutputInvalid
from tests.unit.agents.drive_model_fakes import PROMPT, ScriptedLLM, context, prompt_builder, reply

ANSWER = {
    "title": "Factura FA-0137", "what_it_is": "An electricity invoice for March 2024", "language": "es",
    "doc_type": "invoice", "life_domains": ["home", "finance"],
    "dates": [{"label": "issued", "date": "2024-03-15"}, {"label": "due", "date": "not a date"}],
    "entities": [{"name": "Barcelona", "type": "place", "printed": "Barcelona", "forms": ["Барселона", "Barcelone"]}],
    "key_values": [{"label": "amount_total", "printed": "1.250,00 €", "canonical": "1250.00"}],
    "topics": ["electricity"], "topics_native": ["electricidad"],
    "topic_words": ["electricity", "electricidad", "електроенергія", "электроэнергия"],
    "label_words": ["invoice", "invoices", "factura", "facturas", "рахунок", "рахунки", "счёт", "счета"],
    "domain_words": ["home", "casa"], "validity_words": [],
}


def _request(**kw):
    base = dict(user_id="u1", account_id="acc-1", item_id="item-1", kind="pdf", name="factura.pdf",
                input=ProfileInput(head="Factura nº FA-0137 …", tail="Total 1.250,00 €", headings=("# Factura",)))
    base.update(kw)
    return ProfileRequest(**base)


def _agent(script, builder=None, thinking=None):
    llm = ScriptedLLM(script)
    agent = DriveProfilerAgent(AgentConfig(agent_id="drive_profiler_u1", agent_type=AGENT_TYPE), context(llm),
                               builder or prompt_builder(), thinking=thinking)
    return agent, llm


class TestRequest:
    async def test_schema_mime_type_limits_and_no_storage(self):
        agent, llm = _agent([reply(ANSWER)])
        await agent.profile(_request())
        req = llm.requests[0]
        assert req.system_instruction == PROMPT and req.response_mime_type == "application/json"
        assert req.response_schema is _RESPONSE_SCHEMA
        assert (req.max_tokens, req.timeout, req.thinking) == (DRIVE_PROFILER.max_tokens,
                                                               DRIVE_PROFILER.call_timeout_s, "low")
        assert req.allow_provider_storage is False and req.tools is None

    async def test_the_text_is_data_and_the_task_carries_the_caps(self):
        agent, llm = _agent([reply(ANSWER)])
        await agent.profile(_request(), caps=ListCaps(entities=6, values=6, dates=2, topics=4, words=12))
        text = llm.requests[0].messages[0].parts[0].text
        assert "File name: factura.pdf" in text and "<document>\nFactura nº FA-0137 …" in text
        assert "Total 1.250,00 €\n</document>" in text and "<headings>\n# Factura\n</headings>" in text
        assert "at most 6 entities" in text and "12 words in each word list" in text
        assert "previous" not in text.casefold() and "no key values" not in text

    async def test_the_middle_left_out_is_marked_between_head_and_tail(self):
        agent, llm = _agent([reply(ANSWER)])
        await agent.profile(_request())
        assert "[… the middle of the file is left out …]" in llm.requests[0].messages[0].parts[0].text

    async def test_previous_labels_and_no_key_values(self):
        agent, llm = _agent([reply(ANSWER)])
        await agent.profile(_request(previous=ProfileLabels("invoice", ("finance",), date(2024, 4, 30))),
                            key_values=False)
        text = llm.requests[0].messages[0].parts[0].text
        assert "doc_type invoice" in text and "finance" in text and "2024-04-30" in text
        assert "has no key values" in text

    async def test_folder_contents(self):
        agent, llm = _agent([reply(ANSWER)])
        await agent.profile(_request(kind="folder", input=ProfileInput(head="# Tool", child_names=("src", "a.py"))))
        assert "<folder_contents>\nsrc\na.py\n</folder_contents>" in llm.requests[0].messages[0].parts[0].text

    async def test_a_user_thinking_override_wins(self):
        agent, llm = _agent([reply(ANSWER)], thinking="medium")
        await agent.profile(_request())
        assert llm.requests[0].thinking == "medium"


class TestSchema:
    def test_enums_are_the_fixed_label_set(self):
        p = _RESPONSE_SCHEMA["properties"]
        assert p["doc_type"]["enum"] == list(DOC_TYPES)
        assert p["life_domains"]["items"]["enum"] == list(LIFE_DOMAINS)
        assert p["dates"]["items"]["properties"]["label"]["enum"] == list(DATE_LABELS)
        assert p["entities"]["items"]["properties"]["type"]["enum"] == list(ENTITY_TYPES)
        assert p["key_values"]["items"]["properties"]["label"]["enum"] == list(VALUE_LABELS)

    def test_every_field_is_required_and_nothing_is_nullable(self):
        assert set(_RESPONSE_SCHEMA["required"]) == set(_RESPONSE_SCHEMA["properties"])
        assert "null" not in str(_RESPONSE_SCHEMA) and "nullable" not in str(_RESPONSE_SCHEMA)


class TestParse:
    async def test_the_answer_becomes_an_unverified_profile(self):
        agent, _ = _agent([reply(ANSWER)])
        out = await agent.profile(_request())
        assert out.profile == DriveProfile(
            title="Factura FA-0137", what_it_is="An electricity invoice for March 2024", language="es",
            doc_type="invoice", life_domains=("home", "finance"),
            dates=(ProfileDate("issued", date(2024, 3, 15)),),
            entities=(ProfileEntity("Barcelona", "place", "Barcelona", ("Барселона", "Barcelone")),),
            key_values=(KeyValue("amount_total", "1.250,00 €", "1250.00"),),
            topics=("electricity",), topics_native=("electricidad",), topic_words=tuple(ANSWER["topic_words"]),
            label_words=tuple(ANSWER["label_words"]), domain_words=("home", "casa"), validity_words=())
        assert out.output_limited is False and out.calls == 1

    async def test_lists_are_cut_to_the_caps_asked(self):
        many = dict(ANSWER, topic_words=[f"w{i}" for i in range(30)],
                    entities=[ANSWER["entities"][0]] * 9)
        agent, _ = _agent([reply(many)])
        out = await agent.profile(_request(), caps=ListCaps(entities=6, values=6, dates=2, topics=4, words=12))
        assert len(out.profile.topic_words) == 12 and len(out.profile.entities) == 6

    async def test_key_values_are_dropped_when_the_kind_has_none(self):
        agent, _ = _agent([reply(ANSWER)])
        out = await agent.profile(_request(), key_values=False)
        assert out.profile.key_values == ()

    async def test_invalid_then_valid(self):
        agent, llm = _agent([reply(text="{"), reply(ANSWER)])
        out = await agent.profile(_request())
        assert out.profile.doc_type == "invoice" and out.calls == 2
        assert "not valid" in llm.requests[1].messages[-1].parts[0].text

    @pytest.mark.parametrize("bad", ['[]', '{"title": 1}', '{"title": "t", "dates": "x"}'])
    async def test_still_invalid_after_the_retry(self, bad):
        agent, _ = _agent([reply(text=bad), reply(text=bad)])
        with pytest.raises(DriveOutputInvalid) as caught:
            await agent.profile(_request())
        assert caught.value.model_calls == 2

    async def test_the_output_limit_is_reported_not_parsed(self):
        agent, _ = _agent([reply(text='{"title": "x', finish=FinishReason.MAX_TOKENS)])
        out = await agent.profile(_request())
        assert out.output_limited and out.profile is None and out.calls == 1

    async def test_a_safety_stop_is_a_refusal(self):
        agent, _ = _agent([reply(text="", finish=FinishReason.SAFETY)])
        with pytest.raises(DriveInputRefused):
            await agent.profile(_request())


class TestFailures:
    async def test_a_refusal(self):
        agent, _ = _agent([LLMClientError("no", http_status=400)])
        with pytest.raises(DriveInputRefused):
            await agent.profile(_request())

    async def test_a_server_error_is_an_outage_with_usage(self):
        agent, _ = _agent([LLMServerError("500", http_status=500)])
        with pytest.raises(DriveModelUnavailable) as caught:
            await agent.profile(_request())
        assert caught.value.model_calls == 1 and caught.value.charges_file

    async def test_a_missing_prompt_is_an_outage_without_a_call(self):
        agent, llm = _agent([], prompt_builder(prompt="class X {\n}"))
        with pytest.raises(DriveModelUnavailable) as caught:
            await agent.profile(_request())
        assert caught.value.model_calls == 0 and llm.requests == []


async def test_billed_once_with_the_model_that_served():
    agent, _ = _agent([reply(ANSWER)])
    agent._quota_service = AsyncMock()
    out = await agent.profile(_request())
    assert out.usd == pytest.approx(calculate_cost("claude-haiku-5-5", 1000, 200)) and out.fallback_usd == 0
    assert out.model == "claude-haiku-5-5"
    assert agent._quota_service.record_usage.await_args.kwargs["account_id"] == "acc-1"


def test_halved_caps():
    assert FULL_CAPS.halved() == ListCaps(entities=6, values=6, dates=2, topics=4, words=12)
