"""The connector and the memory agent render a search through the one shared formatter."""
from datetime import datetime, timezone
from unittest.mock import MagicMock

from src.domain.agent import AgentConfig
from src.domain.entities import FactEntity, FactType
from src.domain.fact_query import FactQuery
from src.domain.fact_search_result import PhraseSearchResult
from src.domain.memory_search_format import format_search_result
from src.ports.fact_search_port import FactSearchPort
from src.agents.memory_search_agent import FactsMemoryAgent
from src.domain.agent import AgentIntent, AgentMessage
from tests.support.fact_search_kit import TODAY

FACT = FactEntity(
    id="f-1", account_id="acc-1", created_by_user_id="usr-1", lineage_id="l", text="Clara Moreira's passport is valid "
    "until 2026-12-09", type=FactType.STATE, primary_domain="documents",
    reported_date=datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc))
RESULT = PhraseSearchResult((FACT,), ((0,),))
PHRASE = "Clara's passport validity"


class _Search(FactSearchPort):
    async def search(self, account_id, query, *, exclude=frozenset()):
        raise AssertionError("unused")

    async def search_phrases(self, account_id, queries):
        return RESULT


class _Slots:
    async def query_for_request(self, request, *, path, account_id, user_id):
        return FactQuery(today=TODAY, subject=request, path=path, message=request)


async def test_the_agent_result_is_the_shared_formatters_output():
    agent = FactsMemoryAgent(
        config=AgentConfig(agent_id="facts_memory_agent_usr-1", agent_type="facts_memory"),
        fact_search=_Search(), slots=_Slots(), account_id="acc-1", user_id="usr-1", user_location="")
    message = AgentMessage.create(sender="c", recipient="facts_memory_agent_usr-1", intent=AgentIntent.QUERY,
                                  payload={"query": "passport", "queries": [PHRASE]}, context={})
    response = await agent.execute(message)
    assert response.result == format_search_result([PHRASE], RESULT)
    assert "[q1]" in response.result and "recorded 2026-10-02" in response.result


async def test_the_connector_result_is_the_shared_formatters_output():
    from src.domain.voice_mcp_token import FACTS_TOOL
    from tests.unit.composition.test_mcp_get_user_context_v2 import Resolver, _components, _ctx

    class Agent:
        account_id = "acc-1"

        async def search_facts(self, queries, *, path):
            return RESULT

    tool = _components(Resolver(Agent())).fastmcp._tool_manager.get_tool(FACTS_TOOL)
    text = await tool.run({"queries": [PHRASE]}, context=_ctx())
    assert text == format_search_result([PHRASE], RESULT)
