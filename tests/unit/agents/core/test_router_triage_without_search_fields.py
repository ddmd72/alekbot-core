"""Triage no longer emits the search fields (fact profile RFC §5.4): the schema drops them, the routing metadata has
no `semantic_lens`, and a reply that still carries them routes normally."""
from unittest.mock import AsyncMock, MagicMock

from src.agents.core.router_agent import RouterAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, RoutingMetadata
from src.domain.tone import build_routing_metadata
from src.ports.llm_port import AgentExecutionContext, LLMPort, ProviderCapabilities
from src.ports.prompt_builder_port import PromptBuilderPort
from src.domain.user import PerformanceTier
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience

DROPPED = ("relevant_domains", "semantic_lens", "search_phrase")


def test_the_schema_has_no_search_fields():
    schema = RouterAgent.TRIAGE_RESPONSE_SCHEMA
    for key in DROPPED:
        assert key not in schema["properties"]
        assert key not in schema["required"]


def test_routing_metadata_has_no_semantic_lens():
    meta = build_routing_metadata(
        {"search_intent": "topic", "semantic_lens": ["car"],
         "metadata": {"user_tone": "casual", "task_complexity": "info_search"}})
    assert not hasattr(meta, "semantic_lens")
    assert "semantic_lens" not in meta.to_dict()
    assert isinstance(RoutingMetadata.from_dict({"semantic_lens": ["car"], "reasoning": "x"}), RoutingMetadata)


async def test_a_reply_that_still_carries_the_fields_routes_normally():
    llm = MagicMock(spec=LLMPort)
    llm.generate_content = AsyncMock(return_value=MagicMock(text=(
        '{"needs_memory_search":false,"reasoning":"x","search_intent":"topic","relevant_domains":["possession"],'
        '"semantic_lens":["car"],"search_phrase":"car service",'
        '"metadata":{"user_tone":"casual","task_complexity":"info_search"}}')))
    pb = AsyncMock(spec=PromptBuilderPort)
    pb.build_for_agent.return_value = "Classify."
    ec = AgentExecutionContext(
        agent_type="router", provider=llm, model_name="m", tier=PerformanceTier.ECO,
        capabilities=ProviderCapabilities(), resilience_port=InMemoryProviderResilience())
    router = RouterAgent(
        config=AgentConfig(agent_id="router_agent", agent_type="router"), execution_context=ec,
        coordinator=None, smart_agent_id="smart_agent", prompt_builder=pb)
    message = AgentMessage.create(
        sender="h", recipient="router_agent", intent=AgentIntent.QUERY,
        payload={"text": "When is the car service?"}, context={"user_id": "u", "session_id": "s"})
    response = await router.execute(message)
    assert response.result["routed_to"] == "smart_agent"
