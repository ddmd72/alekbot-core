import pytest

from src.adapters.gemini_adapter import GeminiAdapter
from src.domain.user import PerformanceTier
from src.ports.llm_service import PromptCacheConfig


# ============================================================================
# NEW Provider Refactor Session 6: Gemini capabilities tests
# Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
# ============================================================================
def test_gemini_capabilities():
    adapter = GeminiAdapter(api_key="test-key")
    caps = adapter.get_capabilities()

    assert caps.native_tools is True
    assert caps.context_caching is False
    assert caps.streaming is True
    assert caps.vision is True
    assert caps.max_context_window == 1000000


# ============================================================================
# NEW Provider Refactor Session 6: Tier-to-model mapping tests
# Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
# ============================================================================
def test_gemini_model_for_tier():
    adapter = GeminiAdapter(api_key="test-key")

    assert adapter.get_model_for_tier(PerformanceTier.ECO) == "gemini-flash-lite-latest"
    assert adapter.get_model_for_tier(PerformanceTier.BALANCED) == "gemini-3-flash-preview"
    assert adapter.get_model_for_tier(PerformanceTier.PERFORMANCE) == "gemini-3-pro-preview"


# ============================================================================
# NEW Provider Refactor Session 6: Tier validation tests
# Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
# ============================================================================
def test_gemini_unsupported_tier_raises():
    adapter = GeminiAdapter(api_key="test-key")

    with pytest.raises(ValueError, match="Unsupported tier"):
        adapter.get_model_for_tier("invalid_tier")


# ============================================================================
# MODIFIED Provider Refactor Session 6: Unsupported feature validation tests
# Plan: docs/architecture/provider_refactor/PROVIDER_REFACTOR_EXECUTION_PLAN.md
# ============================================================================
@pytest.mark.asyncio
async def test_gemini_prompt_caching_fails_fast():
    adapter = GeminiAdapter(api_key="test-key")
    cache_config = PromptCacheConfig(enabled=True)

    with pytest.raises(ValueError, match="does not support prompt caching"):
        await adapter.generate_content(
            model_name="gemini-3-flash-preview",
            system_instruction="test",
            messages=[],
            cache_config=cache_config
        )