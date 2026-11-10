"""FactsMemoryAgent on `queries`: phrases in the payload, one slots call per phrase, one scored multi-phrase search."""
import asyncio
from datetime import date
from typing import List

import pytest

from src.domain.fact_search_result import PhraseSearchResult
from src.agents.memory_search_agent import FactsMemoryAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.entities import FactEntity, FactType
from src.domain.fact_query import FactQuery
from src.domain.llm import MessagePart
from src.domain.memory_search_texts import NO_QUERIES_NOTE, NO_RECORDS_TEXT
from src.ports.fact_search_port import FactSearchPort

LOCATION = "Springfield, Portugal"
TODAY = date(2026, 11, 10)


class _Slots:
    """Stands in for FactQuerySlotsAgent: every call waits until all expected calls have started (so a serial caller
    deadlocks and the test times out), then answers with a query carrying its phrase as message."""

    def __init__(self, expected: int, fail_on: str = ""):
        self.expected, self.fail_on, self.calls = expected, fail_on, []
        self._started = asyncio.Event()

    async def query_for_request(self, request, *, path, account_id, user_id):
        self.calls.append((request, path, account_id, user_id))
        if len(self.calls) >= self.expected:
            self._started.set()
        await asyncio.wait_for(self._started.wait(), 1)
        if request == self.fail_on:
            raise RuntimeError("slots exploded")
        return FactQuery(today=TODAY, subject=request, path=path, message=request)


class _Search(FactSearchPort):
    def __init__(self, facts: List[FactEntity] = ()):
        self.facts, self.calls = list(facts), []

    async def search(self, account_id, query, *, exclude=frozenset()):
        raise AssertionError("the agent searches phrases")

    async def search_phrases(self, account_id, queries):
        self.calls.append((account_id, list(queries)))
        return PhraseSearchResult(tuple(self.facts), tuple(() for _ in self.facts))


def _fact(text, **kw):
    return FactEntity(account_id="acc-1", created_by_user_id="usr-1", lineage_id="lin", text=text,
                      type=FactType.STATE, **kw)


def _agent(phrases_expected, facts=(), fail_on=""):
    slots, search = _Slots(phrases_expected, fail_on), _Search(facts)
    agent = FactsMemoryAgent(
        config=AgentConfig(agent_id="facts_memory_agent_usr-1", agent_type="facts_memory"),
        fact_search=search, slots=slots, account_id="acc-1", user_id="usr-1", user_location=LOCATION)
    return agent, slots, search


def _message(payload=None, context=None):
    return AgentMessage.create(
        sender="coordinator", recipient="facts_memory_agent_usr-1", intent=AgentIntent.QUERY,
        payload={"query": "Clara's passport", **(payload or {})}, context=context or {})


PASSPORT = "Clara Moreira's passport is valid until 2026-12-09"
RENEWAL = "Clara Moreira's passport must be renewed by 2026-12-01"


async def test_phrases_are_read_from_the_payload_and_searched_once_in_order():
    agent, slots, search = _agent(2, [_fact(PASSPORT)])

    response = await agent.execute(_message({"queries": [PASSPORT, RENEWAL]}))

    assert response.status == AgentStatus.SUCCESS
    assert len(search.calls) == 1
    assert [q.subject for q in search.calls[0][1]] == [PASSPORT, RENEWAL]
    assert response.metadata["phrase_count"] == 2 and response.metadata["result_count"] == 1


async def test_phrases_put_in_the_context_are_not_read():
    agent, slots, search = _agent(1)

    response = await agent.execute(_message(context={"queries": [PASSPORT]}))

    assert response.result == NO_QUERIES_NOTE
    assert slots.calls == [] and search.calls == []


@pytest.mark.parametrize("queries", [None, [], ["  ", ""], "   "])
async def test_no_phrases_is_a_tool_note_with_no_slots_call_and_no_search(queries):
    agent, slots, search = _agent(1)

    response = await agent.execute(_message({"queries": queries}))

    assert response.status == AgentStatus.SUCCESS
    assert response.result == NO_QUERIES_NOTE
    assert slots.calls == [] and search.calls == []


async def test_one_string_is_one_phrase():
    agent, slots, search = _agent(1)

    await agent.execute(_message({"queries": "Skoda Fabia 2015 insurance policy MP-2026-118204"}))

    assert [c[0] for c in slots.calls] == ["Skoda Fabia 2015 insurance policy MP-2026-118204"]


async def test_a_fifth_phrase_is_dropped_and_logged(caplog):
    phrases = ["Skoda Fabia insurance policy", "Skoda Fabia car service 2026-11-05", PASSPORT, RENEWAL, "fifth"]
    agent, slots, search = _agent(4)

    with caplog.at_level("WARNING"):
        await agent.execute(_message({"queries": phrases}))

    assert [q.subject for q in search.calls[0][1]] == phrases[:4]
    assert "dropped" in caplog.text


async def test_slots_run_concurrently_one_call_per_phrase():
    agent, slots, search = _agent(3)

    await agent.execute(_message({"queries": [PASSPORT, RENEWAL, "dentist check-up Dr. Marta Quillen 2026-10-12"]}))

    assert len(slots.calls) == 3
    assert {c[2:] for c in slots.calls} == {("acc-1", "usr-1")}


async def test_every_query_carries_the_home_and_the_phrase_as_message_without_a_user_message():
    agent, slots, search = _agent(2)

    await agent.execute(_message({"queries": [PASSPORT, RENEWAL]}))

    queries = search.calls[0][1]
    assert {q.home for q in queries} == {LOCATION}
    assert [q.message for q in queries] == [PASSPORT, RENEWAL]


async def test_the_users_latest_message_is_the_message_when_the_turn_has_one():
    agent, slots, search = _agent(2)
    parts = [MessagePart(text="when does Clara's passport expire?")]

    await agent.execute(_message({"queries": [PASSPORT, RENEWAL]}, context={"current_message_parts": parts}))

    assert {q.message for q in search.calls[0][1]} == {"when does Clara's passport expire?"}


async def test_a_voice_call_uses_the_last_user_line_of_its_context():
    agent, slots, search = _agent(1)
    call_context = [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"},
                    {"role": "user", "text": "what did the dentist say?"}]

    await agent.execute(_message({"queries": ["dentist check-up Dr. Marta Quillen 2026-10-12"],
                                  "call_context": call_context}))

    assert search.calls[0][1][0].message == "what did the dentist say?"


async def test_the_need_never_reaches_the_slots_or_the_search():
    agent, slots, search = _agent(1)

    await agent.execute(_message({"query": "NEED-WORDS-ONLY", "queries": [PASSPORT]}))

    assert "NEED-WORDS-ONLY" not in repr(slots.calls) and "NEED-WORDS-ONLY" not in repr(search.calls)


async def test_a_fact_is_rendered_with_context_reported_metadata_and_when():
    fact = _fact("Car service for the 2015 Skoda Fabia", context="Done at the garage", metadata={"policy": "MP-2026-118204"},
                 time_points=[{"from": "2026-11-05", "to": "2026-11-05"}])
    fact = fact.model_copy(update={"reported_date": fact.reported_date.replace(year=2026, month=11, day=6)})
    agent, slots, search = _agent(1, [fact, _fact("Weekend in Coimbra with Clara")])

    response = await agent.execute(_message({"queries": ["car service Skoda Fabia 2026-11-05"]}))

    first, second = response.result.split("\n---\n")
    assert first.splitlines() == [
        "Car service for the 2015 Skoda Fabia", "context: Done at the garage", "reported: 2026-11-06",
        'metadata: {"policy": "MP-2026-118204"}', "when: 2026-11-05"]
    assert second.splitlines()[0] == "Weekend in Coimbra with Clara"


async def test_no_facts_is_the_empty_text():
    agent, slots, search = _agent(1)

    response = await agent.execute(_message({"queries": [PASSPORT]}))

    assert response.status == AgentStatus.SUCCESS and response.result == NO_RECORDS_TEXT


async def test_a_slots_failure_on_one_phrase_still_searches_that_phrase_degraded():
    agent, slots, search = _agent(2, fail_on=RENEWAL)

    response = await agent.execute(_message({"queries": [PASSPORT, RENEWAL]}))

    assert response.status == AgentStatus.SUCCESS
    degraded = search.calls[0][1][1]
    assert degraded.subject == RENEWAL and degraded.message == RENEWAL and degraded.people == ()


async def test_search_facts_is_public_and_takes_phrases_and_a_path():
    agent, slots, search = _agent(1, [_fact(PASSPORT)])

    facts = await agent.search_facts([PASSPORT], path="mcp")

    assert [f.text for f in facts] == [PASSPORT]
    assert search.calls[0][1][0].path == "mcp"


async def test_a_search_error_is_a_failure():
    agent, slots, search = _agent(1)

    async def boom(account_id, queries):
        raise RuntimeError("store down")
    search.search_phrases = boom

    response = await agent.execute(_message({"queries": [PASSPORT]}))

    assert response.status == AgentStatus.FAILED
