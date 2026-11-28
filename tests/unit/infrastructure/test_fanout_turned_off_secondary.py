"""A fan-out whose secondary is turned off for the user is a plain single dispatch of the primary: the merged result's
hint says the listed specialists answered, so a dropped secondary must not be listed. Real coordinator over the seam world;
the specialists are recording stand-ins."""
from __future__ import annotations

from src.domain.agent import AgentMessage, AgentResponse
from src.domain.llm import ToolCall
from src.infrastructure.agent_manifest import SMART_RESPONSE
from src.infrastructure.delegation_engine import DelegationEngine
from tests.support.denied_profile import denying
from tests.support.seam_world import ACCOUNT, USER, SeamWorld


def _answers(text: str):
    def respond(message: AgentMessage) -> AgentResponse:
        return AgentResponse.success(task_id=message.task_id, agent_id=message.recipient, result=text)
    return respond


async def _search(world: SeamWorld, fanout: bool):
    return await DelegationEngine(world.coordinator).dispatch(
        ToolCall(name="delegate_to_specialist", args={"intent": "search_web", "query": "q"}),
        {"user_id": USER, "account_id": ACCOUNT}, {},
        dict(SMART_RESPONSE.intent_fanout) if fanout else {}, "smart_response_agent",
    )


async def test_a_turned_off_secondary_gives_the_plain_single_dispatch_result(monkeypatch):
    plain_world = SeamWorld(monkeypatch)
    plain_world.agent("web_search_agent").always(_answers("web answer"))
    plain = await _search(plain_world, fanout=False)

    world = SeamWorld(monkeypatch)
    world.coordinator._effective_config = denying("maps_query")
    web = world.agent("web_search_agent").always(_answers("web answer"))
    maps = world.agent("maps_search_agent").always(_answers("maps answer"))

    result = await _search(world, fanout=True)

    assert result.result_str == plain.result_str
    assert "Two specialists" not in result.result_str and "Maps" not in result.result_str
    assert result.failed is False
    assert len(web.received) == 1 and maps.received == []


async def test_both_allowed_keeps_the_fanout_and_its_hint(monkeypatch):
    world = SeamWorld(monkeypatch)
    web = world.agent("web_search_agent").always(_answers("web answer"))
    maps = world.agent("maps_search_agent").always(_answers("maps answer"))

    result = await _search(world, fanout=True)

    assert "Two specialists answered" in result.result_str
    assert "web answer" in result.result_str and "maps answer" in result.result_str
    assert len(web.received) == 1 and len(maps.received) == 1
