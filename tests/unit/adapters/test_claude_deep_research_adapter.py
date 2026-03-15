"""
Unit tests for ClaudeDeepResearchAdapter (Cloud Run Jobs delivery model).

Mock boundary: JobRunnerPort (AsyncMock) — the only external call this adapter makes.
Never mock at DeepResearchPort level — that hides translation bugs.

Covers:
- Tier → model mapping (ECO / BALANCED / PERFORMANCE)
- model_override wins over tier
- create_interaction triggers run_job with correct job_name
- JOB_QUERY env var carries the query
- JOB_CONTEXT_JSON env var is valid JSON and carries: user_id, account_id,
  original_query, system_prompt, model, job_id, session_id
- Returned job_id matches job_id embedded in JOB_CONTEXT_JSON
- None system_prompt stored as empty string
- None session_id stored as empty string
- get_status always returns ("in_progress", "")
"""
import json
import uuid
import pytest
from unittest.mock import AsyncMock

from src.adapters.claude_deep_research_adapter import ClaudeDeepResearchAdapter
from src.domain.user import PerformanceTier
from src.ports.job_runner_port import JobRunnerPort


# ============================================================================
# Helpers
# ============================================================================

def _make_adapter(
    job_name: str = "alek-research-job-dev",
    model_override: str | None = None,
) -> tuple[ClaudeDeepResearchAdapter, AsyncMock]:
    runner = AsyncMock(spec=JobRunnerPort)
    runner.run_job.return_value = "operations/run-abc"
    adapter = ClaudeDeepResearchAdapter(
        job_runner=runner,
        job_name=job_name,
        model_override=model_override,
    )
    return adapter, runner


async def _call_create(
    adapter: ClaudeDeepResearchAdapter,
    query: str = "Research Q",
    user_id: str = "u1",
    account_id: str = "acc1",
    original_query: str = "Original Q",
    tier: PerformanceTier = PerformanceTier.BALANCED,
    system_prompt: str | None = "System.",
    session_id: str | None = "sess-1",
) -> str:
    return await adapter.create_interaction(
        query=query,
        user_id=user_id,
        account_id=account_id,
        original_query=original_query,
        tier=tier,
        system_prompt=system_prompt,
        session_id=session_id,
    )


def _get_env_overrides(runner: AsyncMock) -> dict[str, str]:
    return runner.run_job.call_args.kwargs["env_overrides"]


def _get_context(runner: AsyncMock) -> dict:
    overrides = _get_env_overrides(runner)
    return json.loads(overrides["JOB_CONTEXT_JSON"])


# ============================================================================
# Tier → model mapping
# ============================================================================

def test_resolve_model_eco():
    adapter, _ = _make_adapter()
    assert adapter._resolve_model(PerformanceTier.ECO) == "claude-haiku-4-5-20251001"


def test_resolve_model_balanced():
    adapter, _ = _make_adapter()
    assert adapter._resolve_model(PerformanceTier.BALANCED) == "claude-sonnet-4-6"


def test_resolve_model_performance():
    adapter, _ = _make_adapter()
    assert adapter._resolve_model(PerformanceTier.PERFORMANCE) == "claude-opus-4-6"


def test_model_override_wins_over_eco():
    adapter, _ = _make_adapter(model_override="claude-special")
    assert adapter._resolve_model(PerformanceTier.ECO) == "claude-special"


def test_model_override_wins_over_performance():
    adapter, _ = _make_adapter(model_override="claude-special")
    assert adapter._resolve_model(PerformanceTier.PERFORMANCE) == "claude-special"


# ============================================================================
# create_interaction — run_job is called
# ============================================================================

async def test_create_interaction_calls_run_job_once():
    adapter, runner = _make_adapter()
    await _call_create(adapter)
    runner.run_job.assert_awaited_once()


async def test_create_interaction_passes_correct_job_name():
    adapter, runner = _make_adapter(job_name="alek-research-job-prod")
    await _call_create(adapter)
    assert runner.run_job.call_args.kwargs["job_name"] == "alek-research-job-prod"


# ============================================================================
# create_interaction — JOB_QUERY env var
# ============================================================================

async def test_job_query_env_var_present():
    adapter, runner = _make_adapter()
    await _call_create(adapter, query="Test research topic")
    overrides = _get_env_overrides(runner)
    assert "JOB_QUERY" in overrides


async def test_job_query_carries_the_query():
    adapter, runner = _make_adapter()
    await _call_create(adapter, query="Test research topic")
    assert _get_env_overrides(runner)["JOB_QUERY"] == "Test research topic"


# ============================================================================
# create_interaction — JOB_CONTEXT_JSON env var
# ============================================================================

async def test_job_context_json_env_var_present():
    adapter, runner = _make_adapter()
    await _call_create(adapter)
    assert "JOB_CONTEXT_JSON" in _get_env_overrides(runner)


async def test_job_context_json_is_valid_json():
    adapter, runner = _make_adapter()
    await _call_create(adapter)
    raw = _get_env_overrides(runner)["JOB_CONTEXT_JSON"]
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)


async def test_context_carries_user_id():
    adapter, runner = _make_adapter()
    await _call_create(adapter, user_id="user-xyz")
    assert _get_context(runner)["user_id"] == "user-xyz"


async def test_context_carries_account_id():
    adapter, runner = _make_adapter()
    await _call_create(adapter, account_id="acc-xyz")
    assert _get_context(runner)["account_id"] == "acc-xyz"


async def test_context_carries_original_query():
    adapter, runner = _make_adapter()
    await _call_create(adapter, original_query="Bare query without language suffix")
    assert _get_context(runner)["original_query"] == "Bare query without language suffix"


async def test_context_carries_system_prompt():
    adapter, runner = _make_adapter()
    await _call_create(adapter, system_prompt="You are an expert researcher.")
    assert _get_context(runner)["system_prompt"] == "You are an expert researcher."


async def test_context_carries_session_id():
    adapter, runner = _make_adapter()
    await _call_create(adapter, session_id="sess-abc-123")
    assert _get_context(runner)["session_id"] == "sess-abc-123"


async def test_context_carries_resolved_model_for_balanced():
    adapter, runner = _make_adapter()
    await _call_create(adapter, tier=PerformanceTier.BALANCED)
    assert _get_context(runner)["model"] == "claude-sonnet-4-6"


async def test_context_carries_resolved_model_for_performance():
    adapter, runner = _make_adapter()
    await _call_create(adapter, tier=PerformanceTier.PERFORMANCE)
    assert _get_context(runner)["model"] == "claude-opus-4-6"


async def test_context_carries_model_override():
    adapter, runner = _make_adapter(model_override="claude-special")
    await _call_create(adapter)
    assert _get_context(runner)["model"] == "claude-special"


# ============================================================================
# create_interaction — job_id consistency
# ============================================================================

async def test_returns_job_id_as_string():
    adapter, runner = _make_adapter()
    job_id = await _call_create(adapter)
    assert isinstance(job_id, str)
    assert len(job_id) > 0


async def test_returned_job_id_is_valid_uuid():
    adapter, runner = _make_adapter()
    job_id = await _call_create(adapter)
    uuid.UUID(job_id)  # raises ValueError if not valid


async def test_context_job_id_matches_returned_job_id():
    adapter, runner = _make_adapter()
    job_id = await _call_create(adapter)
    assert _get_context(runner)["job_id"] == job_id


async def test_each_call_produces_unique_job_id():
    adapter, _ = _make_adapter()
    id1 = await _call_create(adapter)
    id2 = await _call_create(adapter)
    assert id1 != id2


# ============================================================================
# create_interaction — None/missing optional fields
# ============================================================================

async def test_none_system_prompt_stored_as_empty_string():
    adapter, runner = _make_adapter()
    await _call_create(adapter, system_prompt=None)
    assert _get_context(runner)["system_prompt"] == ""


async def test_none_session_id_stored_as_empty_string():
    adapter, runner = _make_adapter()
    await _call_create(adapter, session_id=None)
    assert _get_context(runner)["session_id"] == ""


# ============================================================================
# get_status — always in_progress
# ============================================================================

async def test_get_status_returns_in_progress():
    adapter, _ = _make_adapter()
    status, payload = await adapter.get_status("some-job-id")
    assert status == "in_progress"


async def test_get_status_payload_is_empty_string():
    adapter, _ = _make_adapter()
    status, payload = await adapter.get_status("some-job-id")
    assert payload == ""


async def test_get_status_does_not_call_job_runner():
    adapter, runner = _make_adapter()
    await adapter.get_status("some-job-id")
    runner.run_job.assert_not_called()
