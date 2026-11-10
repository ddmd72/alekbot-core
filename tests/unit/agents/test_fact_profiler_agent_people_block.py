"""The confirmed people block in the profiler call (fact profile RFC §4.3, §4.5): it goes in the user message before the
fact, the cached system text is the same with and without it, and the quality answer parses. Invented cast only."""
from src.agents.fact_profiler_agent import AGENT_TYPE, FactProfilerAgent
from src.domain.agent import AgentConfig
from src.domain.confirmed_people import parse_confirmed_people, render_people_block
from src.domain.fact_profile_fingerprint import build_profiler_user_message
from src.domain.fact_profiler_reply import ProfilerStatus
from tests.unit.agents.drive_model_fakes import ScriptedLLM, context, prompt_builder, reply

MODEL = "claude-sonnet-5-5"
TEXT = "The user has a daughter, Ana."
PEOPLE = parse_confirmed_people({
    "user": {"name": "Alex Doe", "forms": ["Alex", "Bo"]},
    "people": [{"name": "Ana Doe", "forms": [], "relation": "younger daughter"}]}).entries()
ANSWER = {
    "label_words": [], "time_role": "timeless", "time_points": [], "places": [], "people": [],
    "tax_or_fine": "none", "user_identity": False, "pet": False, "state_or_law": False, "about_person": "none",
    "subject": "lifestyle", "money_flow": False, "use_areas": [], "kinds": [], "sensitivity": "ordinary",
}


def _agent(script):
    llm = ScriptedLLM(script)
    return FactProfilerAgent(AgentConfig(agent_id=AGENT_TYPE, agent_type=AGENT_TYPE), context(llm, MODEL),
                             prompt_builder()), llm


async def test_the_only_user_message_is_the_block_then_the_fact():
    agent, llm = _agent([reply(ANSWER)])
    await agent.profile("acc-1", TEXT, None, PEOPLE)

    messages = llm.requests[0].messages
    assert len(messages) == 1 and messages[0].role == "user"
    assert [p.text for p in messages[0].parts] == [render_people_block(PEOPLE) + f"Fact:\n{TEXT}"]
    assert messages[0].parts[0].text == build_profiler_user_message(TEXT, None, PEOPLE)


async def test_without_people_the_message_is_the_old_one():
    agent, llm = _agent([reply(ANSWER)])
    await agent.profile("acc-1", TEXT, None)
    assert llm.requests[0].messages[0].parts[0].text == f"Fact:\n{TEXT}"


async def test_the_system_instruction_is_the_same_with_and_without_people():
    agent, llm = _agent([reply(ANSWER), reply(ANSWER)])
    await agent.profile("acc-1", TEXT, None, PEOPLE)
    await agent.profile("acc-2", TEXT, None)
    assert llm.requests[0].system_instruction == llm.requests[1].system_instruction
    assert "Confirmed people (" not in llm.requests[0].system_instruction


async def test_a_reply_with_quality_and_review_items_parses():
    flagged = {**ANSWER, "quality": "needs_review", "review_items": [{"field": "people", "mention": "Ana"}]}
    agent, _ = _agent([reply(flagged)])
    result = await agent.profile("acc-1", TEXT, None, PEOPLE)
    assert result.status is ProfilerStatus.OK
    assert result.answer.quality == "needs_review" and result.answer.review_items[0].mention == "Ana"


async def test_an_unknown_quality_is_invalid_and_asked_again_once():
    agent, llm = _agent([reply({**ANSWER, "quality": "unsure"}), reply(ANSWER)])
    result = await agent.profile("acc-1", TEXT, None, PEOPLE)
    assert result.status is ProfilerStatus.OK and result.calls == 2
    assert llm.requests[1].messages[0].parts[0].text == build_profiler_user_message(TEXT, None, PEOPLE)
