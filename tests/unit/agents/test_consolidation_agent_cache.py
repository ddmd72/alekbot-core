import pytest
from unittest.mock import AsyncMock, MagicMock
from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentIntent, AgentStatus
from src.services.agent_context_builder import AgentExecutionContext
from src.domain.request_context import RequestContext
from src.ports.llm_service import LLMService, LLMResponse
from src.ports.llm_service import ProviderCapabilities
from src.domain.user import PerformanceTier
from src.ports.repository import FactRepository
from src.ports.embedding_service import EmbeddingService
from src.services.prompt_builder import PromptBuilder


@pytest.fixture
def mock_deps():
    llm = MagicMock(spec=LLMService)
    llm.generate_content = AsyncMock(return_value=LLMResponse(
        text='```json\n{"new_facts": [{"id": "f1", "content": "test fact", "tags": []}], "new_anchors": []}\n```',
        tool_calls=[]
    ))

    repo = MagicMock(spec=FactRepository)
    repo.get_biographical_context_cached = AsyncMock(return_value=[])
    repo.archive_observations = AsyncMock()
    repo.refresh_biographical_context_cache = AsyncMock()
    repo.add_fact_if_unique = AsyncMock(return_value=(True, "f1"))
    repo.get_active_facts = AsyncMock(return_value=[])

    embedding = MagicMock(spec=EmbeddingService)
    embedding.get_embedding = AsyncMock(return_value=[0.1] * 768)

    prompt_builder = MagicMock(spec=PromptBuilder)
    prompt_builder.build_for_agent = AsyncMock(return_value="CONSOLIDATION PROMPT")
    prompt_builder.invalidate_biographical_cache = MagicMock()

    fact_write_service = AsyncMock()
    fact_write_service.add_facts_batch = AsyncMock(return_value=(1, 0, []))

    return llm, repo, embedding, prompt_builder, fact_write_service


@pytest.mark.asyncio
async def test_consolidation_agent_invalidates_cache_on_success(mock_deps):
    """Cache refresh is triggered when v2 consolidation completes successfully."""
    llm, repo, embedding, prompt_builder, fact_write_service = mock_deps
    user_id = "user-123"

    config = AgentConfig(
        agent_id="consolidation",
        agent_type="consolidation",
        llm_model="gemini"
    )

    execution_context = AgentExecutionContext(
        agent_type="consolidation",
        provider=llm,
        model_name="gemini",
        tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities()
    )

    agent = ConsolidationAgent(
        config=config,
        execution_context=execution_context,
        repository=repo,
        embedding_service=embedding,
        fact_write_service=fact_write_service,
        prompt_builder=prompt_builder
    )

    # Use messages (v2 path) — observation-based flow is deprecated and skips refresh
    msg = AgentMessage.create(
        sender="system",
        recipient="consolidation",
        intent=AgentIntent.DELEGATE,
        payload={"task": "consolidate", "messages": [{"role": "user", "text": "I like hiking"}]},
        context={"user_id": user_id}
    )

    async with RequestContext(user_id=user_id, account_id=user_id):
        response = await agent.execute(msg)

    assert response.status == AgentStatus.SUCCESS

    # Verify PromptBuilder cache was invalidated
    prompt_builder.invalidate_biographical_cache.assert_called_once_with(user_id)

    # Verify Repository cache refresh was triggered (with owner_id=account_id)
    repo.refresh_biographical_context_cache.assert_called_once()
    call_kwargs = repo.refresh_biographical_context_cache.call_args
    assert call_kwargs.kwargs.get("owner_id") == user_id or call_kwargs.args[0] == user_id
