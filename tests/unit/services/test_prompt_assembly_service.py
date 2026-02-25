"""
Unit tests for PromptAssemblyService cache boundary injection.

Verifies that _inject_runtime_context:
- Appends biographical_context block (if non-empty) BEFORE the boundary
- Appends conversation_history block (if non-empty) BEFORE the boundary
- Appends CACHE_BOUNDARY + dynamic content (datetime, Q-S) at end
- Does NOT emit empty wrapper blocks when content is absent
"""

import pytest
from unittest.mock import Mock, AsyncMock

from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService
from src.ports.llm_service import PROMPT_CACHE_BOUNDARY


def _make_service():
    """Create PromptAssemblyService with lightweight mocks."""
    security_port = Mock()
    security_port.validate = AsyncMock(side_effect=lambda text, **_: _make_security_result(text))

    bio_formatter = Mock()
    bio_formatter.format = Mock(side_effect=lambda facts: "\n".join(f"- {f['text']}" for f in facts))

    formatter = Mock()
    formatter.format = Mock(side_effect=lambda hist: "history_content")

    return PromptAssemblyService(
        token_repo=Mock(),
        blueprint_repo=Mock(),
        profile_repo=Mock(),
        security_port=security_port,
        formatter=formatter,
        bio_formatter=bio_formatter,
    )


def _make_security_result(text: str):
    result = Mock()
    result.sanitized_text = text
    result.risk_level = Mock(value="LOW")
    return result


# Blueprint template no longer contains runtime placeholders.
TEMPLATE = (
    "class Alek extends Agent{\n"
    "  properties { static content }\n"
    "}"
)


async def _inject(service, facts=None, history=None, query_specific_context=None):
    return await service._inject_runtime_context(
        prompt=TEMPLATE,
        biographical_facts=facts or [],
        conversation_history=history or [],
        user_id="test_user",
        query_specific_context=query_specific_context,
    )


@pytest.mark.asyncio
async def test_boundary_marker_always_appended():
    """CACHE_BOUNDARY must always appear in the assembled prompt."""
    service = _make_service()
    result = await _inject(service)
    assert PROMPT_CACHE_BOUNDARY in result


@pytest.mark.asyncio
async def test_current_datetime_after_boundary():
    """Datetime value (timezone note) must appear AFTER the boundary marker."""
    service = _make_service()
    result = await _inject(service)
    boundary_pos = result.index(PROMPT_CACHE_BOUNDARY)
    dt_pos = result.index("System time is UTC")
    assert dt_pos > boundary_pos


@pytest.mark.asyncio
async def test_current_datetime_not_in_static_part():
    """The runtime datetime value must NOT appear before the boundary."""
    service = _make_service()
    result = await _inject(service)
    boundary_pos = result.index(PROMPT_CACHE_BOUNDARY)
    static_part = result[:boundary_pos]
    assert "System time is UTC" not in static_part


@pytest.mark.asyncio
async def test_static_biographical_context_before_boundary():
    """Static biographical facts must appear BEFORE the boundary (appended to static section)."""
    service = _make_service()
    static_fact = {"text": "Born in Kyiv", "tags": []}
    result = await _inject(service, facts=[static_fact])
    boundary_pos = result.index(PROMPT_CACHE_BOUNDARY)
    static_part = result[:boundary_pos]
    assert "Born in Kyiv" in static_part


@pytest.mark.asyncio
async def test_biographical_context_absent_when_no_static_facts():
    """biographical_context block must NOT appear when there are no static facts."""
    service = _make_service()
    result = await _inject(service)
    boundary_pos = result.index(PROMPT_CACHE_BOUNDARY)
    static_part = result[:boundary_pos]
    assert "biographical_context" not in static_part


@pytest.mark.asyncio
async def test_query_specific_context_after_boundary_when_present():
    """Pre-formatted Q-S context string must appear AFTER the boundary."""
    service = _make_service()
    result = await _inject(service, query_specific_context="User asked about rates")
    boundary_pos = result.index(PROMPT_CACHE_BOUNDARY)
    dynamic_part = result[boundary_pos:]
    assert "User asked about rates" in dynamic_part


@pytest.mark.asyncio
async def test_query_specific_context_absent_from_dynamic_when_empty():
    """query_specific_context block must NOT appear when there are no semantic facts."""
    service = _make_service()
    result = await _inject(service)
    boundary_pos = result.index(PROMPT_CACHE_BOUNDARY)
    dynamic_part = result[boundary_pos:]
    assert "query_specific_context" not in dynamic_part


@pytest.mark.asyncio
async def test_conversation_history_in_static_when_non_empty():
    """Non-empty conversation history must appear BEFORE the boundary (cached with static prefix).
    This is the consolidation use case: the batch to consolidate is fixed per run and should be cached."""
    service = _make_service()
    result = await _inject(service, history=[{"role": "user", "content": "hi"}])
    boundary_pos = result.index(PROMPT_CACHE_BOUNDARY)
    static_part = result[:boundary_pos]
    assert "history_content" in static_part


@pytest.mark.asyncio
async def test_conversation_history_absent_when_empty():
    """conversation_history block must NOT appear when history is empty."""
    service = _make_service()
    result = await _inject(service)
    boundary_pos = result.index(PROMPT_CACHE_BOUNDARY)
    static_part = result[:boundary_pos]
    assert "conversation_history" not in static_part


def test_normalize_whitespace_removes_empty_blocks():
    """_normalize_whitespace must remove structural blocks left empty by token removal."""
    service = _make_service()
    prompt = "class Alek {\n    policies {\n    }\n\n    protocols {\n    }\n\n    properties { content }\n}"
    result = service._normalize_whitespace(prompt)
    assert "policies" not in result
    assert "protocols" not in result
    assert "properties" in result  # non-empty block preserved


def test_normalize_whitespace_collapses_blank_lines():
    """_normalize_whitespace must collapse 3+ consecutive blank lines to 2."""
    service = _make_service()
    prompt = "line1\n\n\n\nline2"
    result = service._normalize_whitespace(prompt)
    assert "\n\n\n" not in result
    assert "line1" in result
    assert "line2" in result
