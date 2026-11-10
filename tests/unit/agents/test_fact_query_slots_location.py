"""FactQuerySlotsAgent asks the prompt builder for the owner's location on both entry points."""

from src.domain.llm import Message, MessagePart
from tests.unit.agents.test_fact_query_slots_agent import _agent, _slots
from tests.support.scripted_llm import text


async def test_query_for_request_asks_for_the_location_and_keeps_the_life_areas():
    agent, _, builder = _agent([text(_slots(places=[{"city": "Springfield", "country": "Portugal"}]))])
    await agent.query_for_request("who visits me next week", path="fm", account_id="acc-1", user_id="usr-1")
    _, kwargs = builder.kwargs[0]
    assert kwargs["include_location"] is True
    assert kwargs["extra_static_blocks"][0].startswith("LIFE AREAS\n")


async def test_query_for_messages_asks_for_the_location_and_keeps_the_life_areas():
    agent, _, builder = _agent([text(_slots())])
    messages = [Message(role="user", parts=[MessagePart(text="what is planned near home")])]
    await agent.query_for_messages(messages, path="router", account_id="acc-1", user_id="usr-1")
    _, kwargs = builder.kwargs[0]
    assert kwargs["include_location"] is True
    assert kwargs["extra_static_blocks"][0].startswith("LIFE AREAS\n")
