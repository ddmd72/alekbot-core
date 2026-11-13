"""FactQuerySlotsAgent calls its model outside process(): the user it is given must reach the prompt-content row."""
from tests.unit.agents.test_fact_query_slots_agent import _Builder, _agent, _slots
from src.utils.telemetry import get_request_context, request_user_scope


class _Spy:
    def __init__(self):
        self.seen = []

    async def record_turn(self, **kwargs):
        self.seen.append(get_request_context()["user_id"])


async def test_the_call_records_the_given_user_and_restores_the_context():
    agent, _llm, _builder = _agent([_slots_text()])
    spy = _Spy()
    agent._prompt_content_store = spy

    await agent.query_for_request("a request", path="fm", account_id="account-a", user_id="user-a")

    assert spy.seen == ["user-a"]
    assert get_request_context()["user_id"] is None


async def test_no_user_given_keeps_the_outer_user():
    agent, _llm, _builder = _agent([_slots_text()])
    spy = _Spy()
    agent._prompt_content_store = spy

    with request_user_scope("user-outer"):
        await agent.query_for_request("a request", path="fm", account_id="account-a", user_id=None)

    assert spy.seen == ["user-outer"]


def _slots_text():
    from tests.support.scripted_llm import text
    return text(_slots())
