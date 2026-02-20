import pytest

from src.adapters.claude_adapter import ClaudeAdapter
from src.domain.user import PerformanceTier
from src.ports.llm_service import PromptCacheConfig, AutomaticFunctionCallingConfig


# ============================================================================
# NEW Provider Refactor Session 7: Claude capabilities tests
# ============================================================================
def test_claude_capabilities():
    adapter = ClaudeAdapter(api_key="test-key")
    caps = adapter.get_capabilities()

    assert caps.native_tools is False
    assert caps.context_caching is True
    assert caps.streaming is True
    assert caps.vision is True
    assert caps.max_context_window == 200000


# ============================================================================
# NEW Provider Refactor Session 7: Tier-to-model mapping tests
# ============================================================================
def test_claude_model_for_tier():
    adapter = ClaudeAdapter(api_key="test-key")

    assert adapter.get_model_for_tier(PerformanceTier.ECO) == "claude-haiku-4-5"
    assert adapter.get_model_for_tier(PerformanceTier.BALANCED) == "claude-sonnet-4-5"
    assert adapter.get_model_for_tier(PerformanceTier.PERFORMANCE) == "claude-sonnet-4-5"


# ============================================================================
# NEW Provider Refactor Session 7: Tier validation tests
# ============================================================================
def test_claude_unsupported_tier_raises():
    adapter = ClaudeAdapter(api_key="test-key")

    with pytest.raises(ValueError, match="Unsupported tier"):
        adapter.get_model_for_tier("invalid_tier")


# ============================================================================
# MODIFIED Provider Refactor Session 22.1: Automatic function calling blocked
# Claude blocks automatic_function_calling (native tools) but allows manual tools.
# ============================================================================
@pytest.mark.asyncio
async def test_claude_native_tools_fail_fast():
    adapter = ClaudeAdapter(api_key="test-key")

    with pytest.raises(ValueError, match="does not support automatic function calling"):
        await adapter.generate_content(
            model_name="claude-sonnet-4-5",
            system_instruction="test",
            messages=[],
            automatic_function_calling=AutomaticFunctionCallingConfig(enabled=True, mode="AUTO")
        )
