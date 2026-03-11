import pytest
from unittest.mock import AsyncMock
from src.services.prompt_builder import PromptBuilder

@pytest.mark.requirement("REQ-PERF-01")
@pytest.mark.asyncio
async def test_prompt_builder_caching():
    """
    Test that PromptBuilder caches biographical context to minimize latency.
    Covers: REQ-PERF-01 (Context Caching)
    """
    mock_repo = AsyncMock()
    mock_repo.get_biographical_context_cached = AsyncMock(
        return_value=[{"text": "User drives a Honda Civic"}]
    )

    builder = PromptBuilder(mock_repo, cache_ttl=3600)

    # 1. First call — should fetch from repository
    content1 = await builder._get_biographical_component("user-1")
    assert "Honda Civic" in content1
    assert mock_repo.get_biographical_context_cached.call_count == 1

    # 2. Second call immediately — should hit cache (no repo call increase)
    content2 = await builder._get_biographical_component("user-1")
    assert content2 == content1
    assert mock_repo.get_biographical_context_cached.call_count == 1

    # 3. Invalidate the cache
    builder.invalidate_biographical_cache("user-1")

    # 4. After invalidation — should fetch from repository again
    content3 = await builder._get_biographical_component("user-1")
    assert "Honda Civic" in content3
    assert mock_repo.get_biographical_context_cached.call_count == 2
