"""FactQuerySlotsAgent: one cheap-model call becomes a FactQuery; every failure degrades to one semantic list."""
import json
from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest

from src.agents.fact_query_slots_agent import FactQuerySlotsAgent, _RESPONSE_SCHEMA
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.fact_query import FactQuery, QueryPlace, QueryTime
from src.domain.llm import LLMResponse, Message, MessagePart
from src.ports.prompt_builder_port import PromptBuilderPort
from tests.support.scripted_llm import ScriptedLLM, execution_context, text

NOW = datetime(2026, 11, 8, 23, 30, tzinfo=timezone.utc)      # 2026-11-09 00:30 in Berlin, still the 8th in UTC
REQUEST = "when is Clara's passport valid until"


class _Builder(PromptBuilderPort):
    def __init__(self, prompt="PROMPT", error=None):
        self.prompt, self.error, self.kwargs = prompt, error, []

    async def preload_components(self):
        return None

    def invalidate_cache(self, *args, **kwargs):
        return None

    def invalidate_biographical_cache(self, *args, **kwargs):
        return None

    def get_cache_stats(self):
        return {}

    def merge_enriched_context_with_biographical(self, *args, **kwargs):
        return None

    async def build_for_agent(self, agent_type, **kwargs):
        self.kwargs.append((agent_type, kwargs))
        if self.error:
            raise self.error
        return self.prompt


def _slots(**over):
    base = {
        "subject": "Clara's passport validity", "kind": "identity document", "people": ["Clara"], "places": [],
        "time": {"intent": "future", "from": "2026-11-09", "to": ""}, "domains": ["biography"],
        "domain_link": "one_matter", "kinds": ["document"], "scope": "top",
    }
    base.update(over)
    return json.dumps(base)


def _agent(replies, *, builder=None, tz="Europe/Berlin"):
    llm = ScriptedLLM(replies)
    builder = builder or _Builder()
    config = AgentConfig(agent_id="fact_query_slots", agent_type="fact_query_slots")
    agent = FactQuerySlotsAgent(config, execution_context(llm, "fact_query_slots"), builder,
                                user_timezone=tz, clock=lambda: NOW)
    return agent, llm, builder


async def _ask(agent, request=REQUEST, path="fm"):
    return await agent.query_for_request(request, path=path, account_id="acc-1", user_id="usr-1")


def _lines(log):
    return [c.args[0] % c.args[1:] for c in log.info.call_args_list if c.args and "[FactQuery]" in c.args[0]]


# ---- prompt build -----------------------------------------------------------------------------------------------

async def test_the_prompt_is_built_without_biography_directives_or_location_and_with_the_life_areas():
    agent, _, builder = _agent([text(_slots())])
    await _ask(agent)
    agent_type, kwargs = builder.kwargs[0]
    assert agent_type == "fact_query_slots"
    assert kwargs["user_id"] == "usr-1" and kwargs["account_id"] == "acc-1"
    assert kwargs["include_biographical"] is False and kwargs["include_directives"] is False
    assert kwargs["include_location"] is False
    assert len(kwargs["extra_static_blocks"]) == 1 and kwargs["extra_static_blocks"][0].startswith("LIFE AREAS\n")


async def test_the_today_line_follows_the_users_zone_not_the_utc_date():
    agent, llm, _ = _agent([text(_slots())])
    query = await _ask(agent)
    assert llm.requests[0].system_instruction == "PROMPT\nToday: 2026-11-09 (Monday), timezone Europe/Berlin.\n"
    assert query.today == date(2026, 11, 9)


@pytest.mark.parametrize("zone", ["Mars/Olympus", "", None, "../etc"])
async def test_an_invalid_zone_falls_back_to_utc(zone):
    agent, llm, _ = _agent([text(_slots())], tz=zone)
    query = await _ask(agent)
    assert llm.requests[0].system_instruction.endswith("\nToday: 2026-11-08 (Sunday), timezone UTC.\n")
    assert query.today == date(2026, 11, 8)


# ---- request shapes ---------------------------------------------------------------------------------------------

async def test_a_request_is_one_user_message_with_the_search_request_wrapper():
    agent, llm, _ = _agent([text(_slots())])
    await _ask(agent)
    (message,) = llm.requests[0].messages
    assert message.role == "user" and message.parts[0].text == f'SEARCH_REQUEST "{REQUEST}"'


async def test_messages_keep_the_conversation_drop_empty_ones_and_trailing_model_turns():
    def m(role, t):
        return Message(role=role, parts=[MessagePart(text=t)])
    agent, llm, _ = _agent([text(_slots())])
    await agent.query_for_messages(
        [m("user", "plan a trip to Rome"), m("model", "when?"), m("user", "10 to 14 August"), m("model", ""),
         m("model", "noted")],
        path="router", account_id="acc-1", user_id="usr-1",
    )
    sent = [(x.role, x.parts[0].text) for x in llm.requests[0].messages]
    assert sent == [("user", "plan a trip to Rome"), ("model", "when?"), ("user", "10 to 14 August")]


async def test_the_request_fields_are_the_cheap_json_call():
    agent, llm, _ = _agent([text(_slots())])
    await _ask(agent)
    req = llm.requests[0]
    assert req.max_tokens == 2048 and req.disable_safety is True and req.thinking is None
    assert req.tools == [] and req.response_mime_type == "application/json"
    assert req.response_schema == _RESPONSE_SCHEMA
    assert req.model_name == "scripted-model"


# ---- success ----------------------------------------------------------------------------------------------------

async def test_valid_json_becomes_the_query_with_the_path():
    reply = _slots(places=[{"name": "Rome", "country": "Italy"}], scope="all")
    agent, llm, _ = _agent([text(reply)])
    query = await _ask(agent, path="connector")
    assert query == FactQuery(
        today=date(2026, 11, 9), subject="Clara's passport validity", kind="identity document", people=("Clara",),
        places=(QueryPlace("Rome", "Italy"),), time=QueryTime("future", date(2026, 11, 9), None),
        domains=("biography",), kinds=("document",), scope="all", path="connector",
    )
    assert len(llm.requests) == 1 and llm.unanswered == 0


async def test_one_retry_with_the_bad_text_and_a_correction_then_success():
    agent, llm, _ = _agent([text("{not json"), text(_slots())])
    query = await _ask(agent)
    assert query.subject == "Clara's passport validity"
    retry = llm.requests[1].messages
    assert [m.role for m in retry] == ["user", "model", "user"]
    assert retry[1].parts[0].text == "{not json"
    assert "not valid query slots" in retry[2].parts[0].text and "[System" not in retry[2].parts[0].text


async def test_a_json_value_that_is_not_an_object_is_retried_too():
    agent, llm, _ = _agent([text("[1, 2]"), text(_slots())])
    assert (await _ask(agent)).subject == "Clara's passport validity"
    assert len(llm.requests) == 2


# ---- degradation ------------------------------------------------------------------------------------------------

def _semantic(request=REQUEST, path="fm"):
    return FactQuery(today=date(2026, 11, 9), subject=request, path=path)


async def test_two_invalid_outputs_degrade_to_the_semantic_query():
    agent, llm, _ = _agent([text("nope"), text("still nope")])
    with patch("src.agents.fact_query_slots_agent.logger") as log:
        query = await _ask(agent)
    assert query == _semantic() and len(llm.requests) == 2
    assert "outcome=degraded:invalid_output" in _lines(log)[0]


async def test_two_empty_replies_degrade_and_are_named_empty():
    agent, _, _ = _agent([text(""), LLMResponse(text=None, tool_calls=[])])
    with patch("src.agents.fact_query_slots_agent.logger") as log:
        query = await _ask(agent)
    assert query == _semantic() and "outcome=degraded:empty_output" in _lines(log)[0]


async def test_a_call_error_degrades_without_a_retry_and_without_raising():
    agent, llm, _ = _agent([RuntimeError("provider down")])
    with patch("src.agents.fact_query_slots_agent.logger") as log:
        query = await _ask(agent)
    assert query == _semantic() and len(llm.requests) == 1
    assert "outcome=degraded:call_failed" in _lines(log)[0]
    assert log.warning.called


async def test_a_call_error_on_the_retry_degrades_too():
    agent, _, _ = _agent([text("nope"), RuntimeError("provider down")])
    with patch("src.agents.fact_query_slots_agent.logger"):
        assert await _ask(agent) == _semantic()


async def test_a_prompt_build_failure_degrades_logs_an_error_and_makes_no_call():
    agent, llm, _ = _agent([text(_slots())], builder=_Builder(error=KeyError("fact_query_slots")))
    with patch("src.agents.fact_query_slots_agent.logger") as log:
        query = await _ask(agent)
    assert query == _semantic() and llm.requests == []
    assert "[FactQuery] prompt build failed" in log.error.call_args.args[0]
    assert "outcome=degraded:prompt_build_failed" in _lines(log)[0]


async def test_the_degraded_query_on_the_messages_path_is_the_last_user_text():
    def m(role, t):
        return Message(role=role, parts=[MessagePart(text=t)])
    agent, _, _ = _agent([RuntimeError("down")])
    with patch("src.agents.fact_query_slots_agent.logger"):
        query = await agent.query_for_messages(
            [m("user", "first"), m("model", "answer"), m("user", "pollen allergy again"), m("model", "ok")],
            path="router", account_id="acc-1", user_id="usr-1",
        )
    assert query == _semantic("pollen allergy again", path="router")


async def test_no_usable_message_degrades_to_an_empty_subject_without_a_call():
    agent, llm, _ = _agent([text(_slots())])
    with patch("src.agents.fact_query_slots_agent.logger") as log:
        query = await agent.query_for_messages(
            [Message(role="model", parts=[MessagePart(text="hello")])], path="router", account_id=None, user_id=None)
    assert query == _semantic("", path="router") and llm.requests == []
    assert "outcome=degraded:no_messages" in _lines(log)[0]


# ---- log line ---------------------------------------------------------------------------------------------------

async def test_the_log_line_holds_counts_and_names_and_no_slot_values():
    reply = _slots(subject="Alex's car insurance with Example Mutual Insurance", people=["Maria Doe", "Мар"],
                   places=[{"name": "Rome", "country": "Italy"}], domains=["transport", "money", "nonsense"],
                   kinds=["document", "ownership", "bogus"])
    agent, _, _ = _agent([text(reply)])
    with patch("src.agents.fact_query_slots_agent.logger") as log:
        await _ask(agent, request="Example Mutual Insurance policy", path="router")
    (line,) = _lines(log)
    assert line.startswith("[FactQuery] path=router outcome=slots intent=future domains=transport,money ")
    assert "kinds=document,ownership people=1 places=1 scope=top refused=people dropped=2 ms=" in line
    for value in ("Maria", "Rome", "Italy", "Example Mutual", "Clara", "policy"):
        assert value not in line


# ---- delegation surface -----------------------------------------------------------------------------------------

def _message(payload):
    return AgentMessage(task_id="t1", sender="test", recipient="fact_query_slots", intent=AgentIntent.DELEGATE,
                        payload=payload, context={"account_id": "acc-1", "user_id": "usr-1"})


async def test_can_handle_and_execute_map_the_request_payload_to_a_query():
    agent, _, _ = _agent([text(_slots())])
    assert await agent.can_handle(_message({"request": REQUEST})) is True
    assert await agent.can_handle(_message({"task": "x"})) is False
    response = await agent.execute(_message({"request": REQUEST, "path": "voice"}))
    assert response.status is AgentStatus.SUCCESS
    assert response.result["query"].path == "voice" and response.result["query"].people == ("Clara",)


async def test_execute_without_a_request_fails():
    agent, _, _ = _agent([])
    response = await agent.execute(_message({}))
    assert response.status is AgentStatus.FAILED
