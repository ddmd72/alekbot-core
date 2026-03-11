"""
Unit tests for GeminiDeepResearchAdapter.

Pattern: replace adapter._client with MagicMock (synchronous genai.Client mock),
call real adapter method, assert on what was passed to the SDK and what came back.

SDK boundary: self._client.interactions.create / self._client.interactions.get
(synchronous Gemini SDK calls wrapped in asyncio.run_in_executor internally).
"""
import time
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from src.adapters.gemini_deep_research_adapter import (
    GeminiDeepResearchAdapter,
    _CLIENT_MAX_AGE_SECONDS,
)
from src.domain.user import PerformanceTier
from src.ports.task_queue import TaskQueue


# ============================================================================
# Helpers
# ============================================================================

def _make_interaction(id_="interaction-abc", status="in_progress", outputs=None, error=None):
    inter = MagicMock()
    inter.id = id_
    inter.status = status
    inter.outputs = outputs or []
    inter.error = error
    return inter


def _make_adapter(task_queue=None) -> GeminiDeepResearchAdapter:
    """Create adapter with mocked genai.Client (suppresses network I/O in __init__)."""
    with patch("src.adapters.gemini_deep_research_adapter.genai.Client"):
        adapter = GeminiDeepResearchAdapter(api_key="test-key", task_queue=task_queue)
    adapter._client = MagicMock()
    return adapter


# ============================================================================
# Tier → model mapping
# ============================================================================

def test_resolve_model_all_tiers_same():
    adapter = _make_adapter()
    expected = "deep-research-pro-preview-12-2025"
    assert adapter._resolve_model(PerformanceTier.ECO) == expected
    assert adapter._resolve_model(PerformanceTier.BALANCED) == expected
    assert adapter._resolve_model(PerformanceTier.PERFORMANCE) == expected


def test_model_override_wins():
    with patch("src.adapters.gemini_deep_research_adapter.genai.Client"):
        adapter = GeminiDeepResearchAdapter(api_key="test-key", model_override="custom-model")
    assert adapter._resolve_model(PerformanceTier.BALANCED) == "custom-model"


# ============================================================================
# Client lifecycle — proactive refresh (Layer 1)
# ============================================================================

def test_maybe_refresh_client_does_nothing_when_fresh():
    adapter = _make_adapter()
    original_client = adapter._client
    adapter._maybe_refresh_client()
    assert adapter._client is original_client


def test_maybe_refresh_client_recreates_when_stale():
    with patch("src.adapters.gemini_deep_research_adapter.genai.Client") as mock_cls:
        adapter = GeminiDeepResearchAdapter(api_key="test-key")
        # Backdate creation timestamp beyond threshold
        adapter._client_created_at = time.monotonic() - _CLIENT_MAX_AGE_SECONDS - 1
        adapter._maybe_refresh_client()

    # Called once in __init__, once in _maybe_refresh_client
    assert mock_cls.call_count == 2


# ============================================================================
# create_interaction — RuntimeError without task_queue
# ============================================================================

@pytest.mark.asyncio
async def test_create_interaction_raises_without_task_queue():
    adapter = _make_adapter(task_queue=None)

    with pytest.raises(RuntimeError, match="task queue not configured"):
        await adapter.create_interaction(
            query="Q",
            user_id="u1",
            account_id="acc1",
            original_query="Q",
        )


# ============================================================================
# create_interaction — wire tests
# ============================================================================

@pytest.mark.asyncio
async def test_create_interaction_passes_query_and_model():
    task_queue = AsyncMock(spec=TaskQueue)
    task_queue.enqueue_deep_research_polling.return_value = "task-1"
    adapter = _make_adapter(task_queue=task_queue)

    interaction = _make_interaction(id_="job-xyz")
    adapter._client.interactions.create.return_value = interaction

    job_id = await adapter.create_interaction(
        query="Research brief",
        user_id="u1",
        account_id="acc1",
        original_query="Research brief",
        tier=PerformanceTier.BALANCED,
    )

    call_kwargs = adapter._client.interactions.create.call_args.kwargs
    assert call_kwargs["input"] == "Research brief"
    assert call_kwargs["agent"] == "deep-research-pro-preview-12-2025"
    assert call_kwargs["background"] is True
    assert job_id == "job-xyz"


@pytest.mark.asyncio
async def test_create_interaction_returns_interaction_id():
    task_queue = AsyncMock(spec=TaskQueue)
    task_queue.enqueue_deep_research_polling.return_value = "task-1"
    adapter = _make_adapter(task_queue=task_queue)
    adapter._client.interactions.create.return_value = _make_interaction(id_="returned-id")

    job_id = await adapter.create_interaction(
        query="Q",
        user_id="u1",
        account_id="acc1",
        original_query="Q",
    )

    assert job_id == "returned-id"


@pytest.mark.asyncio
async def test_create_interaction_enqueues_polling_task_with_correct_fields():
    task_queue = AsyncMock(spec=TaskQueue)
    task_queue.enqueue_deep_research_polling.return_value = "task-1"
    adapter = _make_adapter(task_queue=task_queue)
    adapter._client.interactions.create.return_value = _make_interaction(id_="job-xyz")

    await adapter.create_interaction(
        query="Q",
        user_id="user-1",
        account_id="acc-1",
        original_query="Original Q",
        session_id="sess-1",
    )

    task_queue.enqueue_deep_research_polling.assert_awaited_once()
    pk = task_queue.enqueue_deep_research_polling.await_args.kwargs
    assert pk["interaction_id"] == "job-xyz"
    assert pk["user_id"] == "user-1"
    assert pk["account_id"] == "acc-1"
    assert pk["provider"] == "gemini"
    assert pk["session_id"] == "sess-1"


# ============================================================================
# get_status — response mapping
# ============================================================================

@pytest.mark.asyncio
async def test_get_status_in_progress():
    adapter = _make_adapter()
    adapter._client.interactions.get.return_value = _make_interaction(status="in_progress")

    status, payload = await adapter.get_status("job-1")

    assert status == "in_progress"
    assert payload == ""


@pytest.mark.asyncio
async def test_get_status_completed_returns_output_text():
    adapter = _make_adapter()
    output = MagicMock()
    output.text = "Final research result."
    interaction = _make_interaction(status="completed", outputs=[output])
    adapter._client.interactions.get.return_value = interaction

    status, payload = await adapter.get_status("job-1")

    assert status == "completed"
    assert payload == "Final research result."


@pytest.mark.asyncio
async def test_get_status_completed_empty_outputs_returns_empty_string():
    adapter = _make_adapter()
    adapter._client.interactions.get.return_value = _make_interaction(
        status="completed", outputs=[]
    )

    status, payload = await adapter.get_status("job-1")

    assert status == "completed"
    assert payload == ""


@pytest.mark.asyncio
async def test_get_status_failed_returns_error():
    adapter = _make_adapter()
    interaction = _make_interaction(status="failed", error="Out of time")
    adapter._client.interactions.get.return_value = interaction

    status, payload = await adapter.get_status("job-1")

    assert status == "failed"
    assert "Out of time" in payload


# ============================================================================
# get_status — Layer 2 reactive retry on stale connection
# ============================================================================

@pytest.mark.asyncio
async def test_get_status_layer2_retry_on_exception():
    """If interactions.get raises, adapter recreates client and retries once."""
    adapter = _make_adapter()

    good_output = MagicMock()
    good_output.text = "Recovered."
    good_interaction = _make_interaction(status="completed", outputs=[good_output])

    # First client raises; new client (created by _recreate_client) succeeds
    adapter._client.interactions.get.side_effect = ConnectionError("stale connection")

    new_client = MagicMock()
    new_client.interactions.get.return_value = good_interaction

    with patch(
        "src.adapters.gemini_deep_research_adapter.genai.Client",
        return_value=new_client,
    ):
        status, payload = await adapter.get_status("job-1")

    assert status == "completed"
    assert payload == "Recovered."
