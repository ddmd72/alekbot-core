"""FactsMemoryAgent.can_handle: a search_memory QUERY without `queries` still reaches `execute` (it answers with a tool
note) instead of falling through to another agent."""
from src.agents.memory_search_agent import FactsMemoryAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.memory_search_texts import NO_QUERIES_NOTE
from src.ports.fact_search_port import FactSearchPort


class _Search(FactSearchPort):
    async def search(self, account_id, query, *, exclude=frozenset()):
        raise AssertionError("no search without phrases")

    async def search_phrases(self, account_id, queries):
        raise AssertionError("no search without phrases")


class _Slots:
    async def query_for_request(self, request, *, path, account_id, user_id):
        raise AssertionError("no slots call without phrases")


def _agent() -> FactsMemoryAgent:
    return FactsMemoryAgent(
        config=AgentConfig(agent_id="facts_memory_agent_usr-1", agent_type="facts_memory"),
        fact_search=_Search(), slots=_Slots(), account_id="acc-1", user_id="usr-1",
        user_location="Springfield, Portugal")


def _query_message(payload: dict, context: dict | None = None) -> AgentMessage:
    return AgentMessage.create(
        sender="coordinator", recipient="facts_memory_agent_usr-1", intent=AgentIntent.QUERY,
        payload=payload, context=context or {})


async def test_a_query_message_whose_context_has_no_queries_is_handled():
    message = _query_message({"query": "Clara Moreira's passport"}, context={"account_id": "acc-1"})

    assert "queries" not in message.context
    assert await _agent().can_handle(message) is True


async def test_such_a_message_comes_back_as_the_tool_note():
    agent = _agent()
    message = _query_message({"query": "Clara Moreira's passport"})

    assert await agent.can_handle(message) is True
    response = await agent.execute(message)

    assert response.status == AgentStatus.SUCCESS
    assert response.result == NO_QUERIES_NOTE
