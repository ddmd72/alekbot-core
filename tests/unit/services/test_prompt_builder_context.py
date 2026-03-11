import pytest
from unittest.mock import AsyncMock, MagicMock
from src.services.prompt_builder import PromptBuilder
from src.domain.prompt import TEMPLATE_FULL
from src.ports.repository import FactRepository
from src.ports.llm_port import ProviderCapabilities
from src.domain.agent import RoutingMetadata

@pytest.fixture
def mock_repo():
    repo = MagicMock(spec=FactRepository)
    repo.get_biographical_context_cached = AsyncMock(return_value=[
        {"text": "Fact 1", "type": "EVENT", "tags": [], "created_at": "2026-01-01"},
        {"text": "Fact 2", "type": "STATE", "tags": [], "created_at": "2026-01-02"}
    ])
    repo.get_latest_fact_by_lineage = AsyncMock(side_effect=_get_mock_fact)
    repo.get_active_facts = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def mock_assembly_service():
    svc = MagicMock()
    svc.assemble = AsyncMock(return_value="ASSEMBLED PROMPT RESULT")
    return svc


def _get_mock_fact(owner_id: str, lineage_id: str):
    if lineage_id == "kernel":
        return MagicMock(
            text=(
                "class Alek extends Agent {\n"
                "  knowledge_base {\n"
                "    biographical_context: '''\n"
                "// Runtime injection placeholder\n"
                "'''\n"
                "  }\n"
                "}\n"
                "\n"
                "class AlekWithTools extends Alek {}\n"
            )
        )
    if lineage_id == "kernel_light":
        return MagicMock(
            text=(
                "class Alek extends Agent {\n"
                "  knowledge_base {\n"
                "    biographical_context: '''\n"
                "// Runtime injection placeholder\n"
                "'''\n"
                "  }\n"
                "}\n"
            )
        )
    if lineage_id == "examples":
        return MagicMock(text="Example 1")
    return MagicMock(text="")

@pytest.mark.asyncio
async def test_build_system_prompt_calls_repo(mock_repo):
    builder = PromptBuilder(mock_repo)
    user_id = "test-user-123"
    
    result = await builder.build_system_prompt(mode="full", user_id=user_id)
    
    mock_repo.get_biographical_context_cached.assert_called_once_with(owner_id=user_id, limit=100)
    assert "- Fact 1" in result["biographical_context"]
    assert "- Fact 2" in result["biographical_context"]

@pytest.mark.asyncio
async def test_biographical_context_caching(mock_repo):
    builder = PromptBuilder(mock_repo)
    user_id = "test-user-123"
    
    # First call
    await builder.build_system_prompt(mode="full", user_id=user_id)
    # Second call
    await builder.build_system_prompt(mode="full", user_id=user_id)
    
    # Should only be called once due to internal cache
    mock_repo.get_biographical_context_cached.assert_called_once()

@pytest.mark.asyncio
async def test_biographical_cache_invalidation(mock_repo):
    builder = PromptBuilder(mock_repo)
    user_id = "test-user-123"
    
    # First call
    await builder.build_system_prompt(mode="full", user_id=user_id)
    
    # Invalidate
    builder.invalidate_biographical_cache(user_id)
    
    # Second call
    await builder.build_system_prompt(mode="full", user_id=user_id)
    
    # Should be called twice
    assert mock_repo.get_biographical_context_cached.call_count == 2


@pytest.mark.asyncio
async def test_build_for_agent_quick_mode(mock_repo, mock_assembly_service):
    """build_for_agent delegates to assembly_service.assemble() with agent_type."""
    builder = PromptBuilder(mock_repo, assembly_service=mock_assembly_service)
    prompt = await builder.build_for_agent(agent_type="quick", user_id="user1", account_id="acct1")

    mock_assembly_service.assemble.assert_called_once()
    kwargs = mock_assembly_service.assemble.call_args.kwargs
    assert kwargs["agent_type"] == "quick"
    assert prompt == "ASSEMBLED PROMPT RESULT"


@pytest.mark.asyncio
async def test_build_for_agent_smart_mode(mock_repo, mock_assembly_service):
    """build_for_agent delegates to assembly_service.assemble() with agent_type=smart."""
    builder = PromptBuilder(mock_repo, assembly_service=mock_assembly_service)
    prompt = await builder.build_for_agent(agent_type="smart", user_id="user1", account_id="acct1")

    mock_assembly_service.assemble.assert_called_once()
    kwargs = mock_assembly_service.assemble.call_args.kwargs
    assert kwargs["agent_type"] == "smart"
    assert prompt == "ASSEMBLED PROMPT RESULT"


@pytest.mark.asyncio
async def test_build_for_agent_fetches_biographical_facts_by_account(mock_repo, mock_assembly_service):
    """Biographical facts are fetched via account_id (OAuth multi-tenant separation)."""
    builder = PromptBuilder(mock_repo, assembly_service=mock_assembly_service)
    await builder.build_for_agent(agent_type="quick", user_id="user1", account_id="acct1")

    mock_repo.get_biographical_context_cached.assert_called_once_with("acct1")
    kwargs = mock_assembly_service.assemble.call_args.kwargs
    assert len(kwargs["biographical_facts"]) == 2  # 2 facts from mock_repo


@pytest.mark.asyncio
async def test_build_for_agent_applies_serious_tone(mock_repo, mock_assembly_service):
    """routing_metadata is accepted without error (stored for assembly context)."""
    builder = PromptBuilder(mock_repo, assembly_service=mock_assembly_service)
    routing_metadata = RoutingMetadata.from_dict({"user_tone": "serious"})
    prompt = await builder.build_for_agent(
        agent_type="quick",
        user_id="user1",
        routing_metadata=routing_metadata
    )
    assert prompt == "ASSEMBLED PROMPT RESULT"


@pytest.mark.asyncio
async def test_build_for_agent_strips_tools_when_unsupported(mock_repo, mock_assembly_service):
    """capabilities param is accepted without error; actual rendering is assembly_service concern."""
    builder = PromptBuilder(mock_repo, assembly_service=mock_assembly_service)
    capabilities = ProviderCapabilities(native_tools=False)
    prompt = await builder.build_for_agent(
        agent_type="smart",
        user_id="user1",
        capabilities=capabilities
    )
    assert prompt == "ASSEMBLED PROMPT RESULT"


@pytest.mark.asyncio
async def test_component_service_injects_biographical_context(mock_repo, mock_assembly_service):
    """When biographical_facts provided, they are passed to assembly_service unchanged."""
    builder = PromptBuilder(mock_repo, assembly_service=mock_assembly_service)
    custom_facts = [{"text": "Fact 1"}, {"text": "Fact 2"}]
    await builder.build_for_agent(
        agent_type="smart",
        user_id="user1",
        biographical_facts=custom_facts
    )

    # Repo should NOT be called when facts provided
    mock_repo.get_biographical_context_cached.assert_not_called()
    kwargs = mock_assembly_service.assemble.call_args.kwargs
    assert kwargs["biographical_facts"] == custom_facts
