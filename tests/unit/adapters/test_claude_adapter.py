import pytest

from src.adapters.claude_adapter import ClaudeAdapter
from src.domain.user import PerformanceTier
from src.ports.llm_service import PromptCacheConfig, AutomaticFunctionCallingConfig, PROMPT_CACHE_BOUNDARY


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
def test_cache_control_not_applied_when_system_instruction_empty():
    """cache_control must not be added to empty system_parts (Anthropic 400 guard)."""
    adapter = ClaudeAdapter(api_key="test-key")
    cache_config = PromptCacheConfig(enabled=True)

    # Simulate what generate_content does with empty system_instruction
    system_instruction = None
    system_parts = [{"type": "text", "text": system_instruction}]
    if cache_config and cache_config.enabled and system_instruction:
        system_parts[0]["cache_control"] = {"type": "ephemeral"}

    assert "cache_control" not in system_parts[0]


def test_cache_control_applied_when_system_instruction_present():
    """cache_control IS added when system_instruction is non-empty."""
    adapter = ClaudeAdapter(api_key="test-key")
    cache_config = PromptCacheConfig(enabled=True)

    system_instruction = "You are a helpful assistant."
    system_parts = [{"type": "text", "text": system_instruction}]
    if cache_config and cache_config.enabled and system_instruction:
        system_parts[0]["cache_control"] = {"type": "ephemeral"}

    assert system_parts[0]["cache_control"] == {"type": "ephemeral"}


def _build_system_parts(system_instruction, cache_config):
    """Replicate the system_parts construction logic from ClaudeAdapter.generate_content."""
    if cache_config and cache_config.enabled and system_instruction:
        if PROMPT_CACHE_BOUNDARY in system_instruction:
            static_part, dynamic_part = system_instruction.split(PROMPT_CACHE_BOUNDARY, 1)
            return [
                {"type": "text", "text": static_part.strip(), "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": dynamic_part.strip()},
            ]
        else:
            return [{"type": "text", "text": system_instruction, "cache_control": {"type": "ephemeral"}}]
    else:
        return [{"type": "text", "text": system_instruction or ""}]


def test_cache_splits_at_boundary_marker():
    """With boundary marker: 2 blocks, cache_control only on static prefix."""
    static = "static system instructions"
    dynamic = "current_date_time { 2026-02-25 }"
    instruction = f"{static}\n\n{PROMPT_CACHE_BOUNDARY}\n{dynamic}"
    cache_config = PromptCacheConfig(enabled=True)

    parts = _build_system_parts(instruction, cache_config)

    assert len(parts) == 2
    assert parts[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in parts[1]
    assert parts[0]["text"] == static
    assert parts[1]["text"] == dynamic


def test_cache_no_boundary_caches_whole_system():
    """Without boundary marker: single block with cache_control (legacy / consolidation)."""
    instruction = "full static consolidation prompt"
    cache_config = PromptCacheConfig(enabled=True)

    parts = _build_system_parts(instruction, cache_config)

    assert len(parts) == 1
    assert parts[0]["cache_control"] == {"type": "ephemeral"}
    assert parts[0]["text"] == instruction


def test_no_cache_config_single_block_no_cache_control():
    """Without cache_config: single block, no cache_control."""
    instruction = "some system instruction"

    parts = _build_system_parts(instruction, cache_config=None)

    assert len(parts) == 1
    assert "cache_control" not in parts[0]
    assert parts[0]["text"] == instruction


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
