"""`queries` of search_memory: declared on the delegate tool, declared in Lelik's session on a call without the MCP
servers, and carried by the real coordinator into the agent's payload."""
from datetime import date
from unittest.mock import AsyncMock, MagicMock

from src.domain.fact_search_result import PhraseSearchResult
from src.agents.base_agent import BaseAgent
from src.agents.lelik_agent import LelikAgent
from src.agents.memory_search_agent import FactsMemoryAgent
from src.domain.agent import AgentConfig, AgentStatus
from src.domain.fact_query import FactQuery
from src.domain.lelik_context import LelikContext
from src.domain.memory_search_texts import QUERIES_FIELD_TEXT
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS, MEMORY_SEARCH, Intent
from src.infrastructure.agent_registry import AgentRegistry
from src.ports.fact_search_port import FactSearchPort


def _registry() -> AgentRegistry:
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    return registry


def _queries_property(declaration):
    return declaration["parameters"]["properties"]["context"]["properties"]["queries"]


# The manifest's declared field text: a delegate-only lead (search_memory reads these phrases, never `query`) followed
# by the shared QUERIES_FIELD_TEXT the connector also reads, whole and unchanged.
DECLARED = MEMORY_SEARCH.context_schemas[Intent.SEARCH_MEMORY]["queries"]["description"]


def test_the_delegate_tool_carries_queries_as_an_array_of_strings():
    declaration = BaseAgent._build_delegate_tool_declaration(_registry().get_available_intents())

    queries = _queries_property(declaration)
    assert queries["type"] == "array" and queries["items"] == {"type": "string"}
    assert queries["description"] == DECLARED
    assert DECLARED.endswith(QUERIES_FIELD_TEXT) and DECLARED != QUERIES_FIELD_TEXT


def test_the_manifest_describes_search_memory_by_its_own_text():
    assert MEMORY_SEARCH.capability_descriptions[Intent.SEARCH_MEMORY]
    assert "queries" in MEMORY_SEARCH.context_schemas[Intent.SEARCH_MEMORY]


async def test_lelik_declares_queries_on_a_call_without_the_mcp_servers():
    prompt_builder = AsyncMock()
    prompt_builder.build_for_agent.return_value = "you are Lelik"
    persona = AsyncMock()
    persona.assemble.return_value = LelikContext(biographical_facts=[], conversation_history=[])
    agent = LelikAgent(
        config=MagicMock(agent_id="lelik_agent_usr-1"), telephony=AsyncMock(), from_number="+1",
        status_callback_url="https://x/voice/status", prompt_builder=prompt_builder, persona=persona,
        notifications=AsyncMock(), voice_profile=VOICE_PROVIDER_PROFILES["xai"], mcp_servers=(),
    )
    agent.coordinator = AgentCoordinator(registry=_registry(), task_queue=None, alert_sink=AsyncMock())

    config = await agent.session_config("usr-1", "acc-1")

    delegate = next(t for t in config["tools"] if t["name"] == "delegate_to_specialist")
    assert _queries_property(delegate)["description"] == DECLARED
    assert DECLARED.endswith(QUERIES_FIELD_TEXT)


class _Slots:
    async def query_for_request(self, request, *, path, account_id, user_id):
        return FactQuery(today=date(2026, 11, 10), subject=request, path=path, message=request)


class _Search(FactSearchPort):
    def __init__(self):
        self.calls = []

    async def search(self, account_id, query, *, exclude=frozenset()):
        raise AssertionError

    async def search_phrases(self, account_id, queries):
        self.calls.append(list(queries))
        return PhraseSearchResult((), ())


async def test_a_delegation_with_queries_arrives_in_the_agents_payload_through_the_real_coordinator():
    search = _Search()
    coordinator = AgentCoordinator(registry=_registry())
    coordinator.register_agent(FactsMemoryAgent(
        config=AgentConfig(agent_id="facts_memory_agent_usr-1", agent_type="facts_memory"),
        fact_search=search, slots=_Slots(), account_id="acc-1", user_id="usr-1", user_location="Springfield, Portugal"))

    response = await coordinator.handle_delegation(
        intent=Intent.SEARCH_MEMORY, query="car insurance",
        context={"user_id": "usr-1", "account_id": "acc-1",
                 "params": {"queries": ["2015 Skoda Fabia insurance policy MP-2026-118204",
                                        "2015 Skoda Fabia car service 2026-11-05"]}})

    assert response.status == AgentStatus.SUCCESS
    assert [q.subject for q in search.calls[0]] == [
        "2015 Skoda Fabia insurance policy MP-2026-118204", "2015 Skoda Fabia car service 2026-11-05"]
