"""
Unit tests for OpenAIDeepResearchAdapter.

Pattern: replace adapter._client with AsyncMock, call real adapter method,
assert on what was passed to the SDK and what came back.

SDK boundary: self._client.responses.create / self._client.responses.retrieve.
"""
import pytest
from unittest.mock import MagicMock, AsyncMock

from src.adapters.openai_deep_research_adapter import OpenAIDeepResearchAdapter
from src.domain.user import PerformanceTier


# ============================================================================
# Helpers
# ============================================================================

def _make_response(id_="resp-abc", status="completed", output_text="Result", error=None):
    r = MagicMock()
    r.id = id_
    r.status = status
    r.output_text = output_text
    r.error = error
    return r


def _make_adapter(webhook_url=None, model_override=None) -> OpenAIDeepResearchAdapter:
    adapter = OpenAIDeepResearchAdapter(
        api_key="test-key",
        webhook_url=webhook_url,
        model_override=model_override,
    )
    adapter._client = AsyncMock()
    return adapter


# ============================================================================
# Tier → model mapping
# ============================================================================

def test_resolve_model_eco():
    adapter = _make_adapter()
    assert adapter._resolve_model(PerformanceTier.ECO) == "o4-mini-deep-research-2025-06-26"


def test_resolve_model_balanced():
    adapter = _make_adapter()
    assert adapter._resolve_model(PerformanceTier.BALANCED) == "o4-mini-deep-research-2025-06-26"


def test_resolve_model_performance():
    adapter = _make_adapter()
    assert adapter._resolve_model(PerformanceTier.PERFORMANCE) == "o3-deep-research-2025-06-26"


def test_model_override_wins():
    adapter = _make_adapter(model_override="custom-research-model")
    assert adapter._resolve_model(PerformanceTier.PERFORMANCE) == "custom-research-model"


# ============================================================================
# create_interaction — wire tests
# ============================================================================

@pytest.mark.asyncio
async def test_create_interaction_returns_response_id():
    adapter = _make_adapter()
    adapter._client.responses.create.return_value = _make_response(id_="resp-1")

    job_id = await adapter.create_interaction(
        query="Research brief",
        user_id="u1",
        account_id="acc1",
        original_query="Brief",
        tier=PerformanceTier.BALANCED,
    )

    assert job_id == "resp-1"


@pytest.mark.asyncio
async def test_create_interaction_sends_correct_model_for_performance():
    adapter = _make_adapter()
    adapter._client.responses.create.return_value = _make_response()

    await adapter.create_interaction(
        query="Q",
        user_id="u1",
        account_id="acc1",
        original_query="Q",
        tier=PerformanceTier.PERFORMANCE,
    )

    kwargs = adapter._client.responses.create.await_args.kwargs
    assert kwargs["model"] == "o3-deep-research-2025-06-26"


@pytest.mark.asyncio
async def test_create_interaction_sends_correct_model_for_balanced():
    adapter = _make_adapter()
    adapter._client.responses.create.return_value = _make_response()

    await adapter.create_interaction(
        query="Q",
        user_id="u1",
        account_id="acc1",
        original_query="Q",
        tier=PerformanceTier.BALANCED,
    )

    kwargs = adapter._client.responses.create.await_args.kwargs
    assert kwargs["model"] == "o4-mini-deep-research-2025-06-26"


@pytest.mark.asyncio
async def test_create_interaction_sends_background_true():
    adapter = _make_adapter()
    adapter._client.responses.create.return_value = _make_response()

    await adapter.create_interaction(
        query="Q",
        user_id="u1",
        account_id="acc1",
        original_query="Q",
    )

    kwargs = adapter._client.responses.create.await_args.kwargs
    assert kwargs["background"] is True


@pytest.mark.asyncio
async def test_create_interaction_sends_web_search_preview_tool():
    """web_search_preview tool must always be included for deep research requests."""
    adapter = _make_adapter()
    adapter._client.responses.create.return_value = _make_response()

    await adapter.create_interaction(
        query="Q",
        user_id="u1",
        account_id="acc1",
        original_query="Q",
    )

    kwargs = adapter._client.responses.create.await_args.kwargs
    tools = kwargs.get("tools", [])
    assert any(t.get("type") == "web_search_preview" for t in tools), (
        f"web_search_preview missing from tools: {tools}"
    )


@pytest.mark.asyncio
async def test_create_interaction_metadata_carries_routing_info():
    """user_id, account_id, session_id must be embedded in metadata for webhook routing."""
    adapter = _make_adapter()
    adapter._client.responses.create.return_value = _make_response()

    await adapter.create_interaction(
        query="Q",
        user_id="user-1",
        account_id="acc-1",
        original_query="Original Q",
        session_id="sess-42",
    )

    kwargs = adapter._client.responses.create.await_args.kwargs
    meta = kwargs["metadata"]
    assert meta["user_id"] == "user-1"
    assert meta["account_id"] == "acc-1"
    assert meta["session_id"] == "sess-42"
    assert "Original Q" in meta["query"]


@pytest.mark.asyncio
async def test_create_interaction_query_truncated_to_512_in_metadata():
    """original_query in metadata is capped at 512 characters."""
    adapter = _make_adapter()
    adapter._client.responses.create.return_value = _make_response()
    long_query = "x" * 1000

    await adapter.create_interaction(
        query="Q",
        user_id="u1",
        account_id="acc1",
        original_query=long_query,
    )

    kwargs = adapter._client.responses.create.await_args.kwargs
    assert len(kwargs["metadata"]["query"]) == 512


# ============================================================================
# get_status — response mapping
# ============================================================================

@pytest.mark.asyncio
async def test_get_status_in_progress():
    adapter = _make_adapter()
    adapter._client.responses.retrieve.return_value = _make_response(
        status="in_progress", output_text=""
    )

    status, payload = await adapter.get_status("resp-1")

    assert status == "in_progress"
    assert payload == ""


@pytest.mark.asyncio
async def test_get_status_completed_returns_output_text():
    adapter = _make_adapter()
    adapter._client.responses.retrieve.return_value = _make_response(
        status="completed", output_text="The research result"
    )

    status, payload = await adapter.get_status("resp-1")

    assert status == "completed"
    assert payload == "The research result"


@pytest.mark.asyncio
async def test_get_status_failed_returns_error_string():
    adapter = _make_adapter()
    adapter._client.responses.retrieve.return_value = _make_response(
        status="failed", output_text=None, error="Timeout"
    )

    status, payload = await adapter.get_status("resp-1")

    assert status == "failed"
    assert "Timeout" in payload


@pytest.mark.asyncio
async def test_get_status_failed_no_error_returns_unknown():
    """get_status with failed status and no error attr → 'unknown error'."""
    adapter = _make_adapter()
    response = _make_response(status="failed", output_text=None, error=None)
    adapter._client.responses.retrieve.return_value = response

    status, payload = await adapter.get_status("resp-1")

    assert status == "failed"
    assert payload == "unknown error"
