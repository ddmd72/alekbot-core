"""
Wire tests for CloudRunJobsAdapter.

Mock boundary: aiohttp.ClientSession (HTTP layer) + asyncio.to_thread (ADC token refresh).
Never mock at JobRunnerPort level — that would hide translation bugs.

Covers:
- URL construction (project, region, job_name, :run suffix)
- Request body shape (containerOverrides env list)
- Authorization header (Bearer token)
- Return value (operation name from response JSON)
- HTTP error handling (non-2xx → RuntimeError)
- Multiple env overrides all present
- Empty env overrides
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.adapters.cloud_run_jobs_adapter import CloudRunJobsAdapter


# ============================================================================
# Helpers
# ============================================================================

def _make_adapter(project="test-project", region="us-central1"):
    return CloudRunJobsAdapter(project=project, region=region)


def _mock_resp(ok=True, json_data=None, text="Error body", status=200):
    resp = AsyncMock()
    resp.ok = ok
    resp.status = status
    resp.json.return_value = {"name": "operations/run-abc-123"} if json_data is None else json_data
    resp.text.return_value = text
    return resp


def _mock_session(resp):
    """Build a mock aiohttp.ClientSession async context manager.

    session.post must be a plain MagicMock (not AsyncMock): calling session.post(...)
    must return an async context manager directly, not a coroutine.
    aiohttp uses `async with session.post(...) as resp:`, not `await session.post(...)`.
    """
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = False

    post_cm = AsyncMock()
    post_cm.__aenter__.return_value = resp
    post_cm.__aexit__.return_value = False

    session.post = MagicMock(return_value=post_cm)
    return session


def _patch_auth(token="test-token"):
    return patch(
        "src.adapters.cloud_run_jobs_adapter.asyncio.to_thread",
        new=AsyncMock(return_value=token),
    )


# ============================================================================
# Initialization
# ============================================================================

def test_init_stores_project():
    adapter = _make_adapter(project="my-proj")
    assert adapter._project == "my-proj"


def test_init_stores_region():
    adapter = _make_adapter(region="us-east1")
    assert adapter._region == "us-east1"


# ============================================================================
# run_job — URL construction
# ============================================================================

async def test_run_job_url_contains_project():
    resp = _mock_resp()
    session = _mock_session(resp)
    with patch("aiohttp.ClientSession", return_value=session), _patch_auth():
        adapter = _make_adapter(project="proj-123")
        await adapter.run_job("any-job", {})

    url = session.post.call_args[0][0]
    assert "proj-123" in url


async def test_run_job_url_contains_region():
    resp = _mock_resp()
    session = _mock_session(resp)
    with patch("aiohttp.ClientSession", return_value=session), _patch_auth():
        adapter = _make_adapter(region="europe-west1")
        await adapter.run_job("any-job", {})

    url = session.post.call_args[0][0]
    assert "europe-west1" in url


async def test_run_job_url_contains_job_name():
    resp = _mock_resp()
    session = _mock_session(resp)
    with patch("aiohttp.ClientSession", return_value=session), _patch_auth():
        adapter = _make_adapter()
        await adapter.run_job("alek-research-job-dev", {})

    url = session.post.call_args[0][0]
    assert "alek-research-job-dev" in url


async def test_run_job_url_ends_with_run():
    resp = _mock_resp()
    session = _mock_session(resp)
    with patch("aiohttp.ClientSession", return_value=session), _patch_auth():
        adapter = _make_adapter()
        await adapter.run_job("my-job", {})

    url = session.post.call_args[0][0]
    assert url.endswith(":run")


# ============================================================================
# run_job — request body shape
# ============================================================================

async def test_run_job_body_env_overrides_shape():
    resp = _mock_resp()
    session = _mock_session(resp)
    with patch("aiohttp.ClientSession", return_value=session), _patch_auth():
        adapter = _make_adapter()
        await adapter.run_job("job-name", {"JOB_QUERY": "research", "JOB_CONTEXT_JSON": "{}"})

    body = session.post.call_args.kwargs["json"]
    env_list = body["overrides"]["containerOverrides"][0]["env"]
    env_map = {e["name"]: e["value"] for e in env_list}
    assert env_map["JOB_QUERY"] == "research"
    assert env_map["JOB_CONTEXT_JSON"] == "{}"


async def test_run_job_empty_env_overrides_sends_empty_list():
    resp = _mock_resp()
    session = _mock_session(resp)
    with patch("aiohttp.ClientSession", return_value=session), _patch_auth():
        adapter = _make_adapter()
        await adapter.run_job("job-name", {})

    body = session.post.call_args.kwargs["json"]
    env_list = body["overrides"]["containerOverrides"][0]["env"]
    assert env_list == []


async def test_run_job_multiple_env_overrides_all_present():
    resp = _mock_resp()
    session = _mock_session(resp)
    overrides = {"A": "1", "B": "2", "C": "3"}
    with patch("aiohttp.ClientSession", return_value=session), _patch_auth():
        adapter = _make_adapter()
        await adapter.run_job("job-name", overrides)

    body = session.post.call_args.kwargs["json"]
    env_list = body["overrides"]["containerOverrides"][0]["env"]
    env_map = {e["name"]: e["value"] for e in env_list}
    assert env_map == overrides


async def test_run_job_body_has_container_overrides_wrapper():
    resp = _mock_resp()
    session = _mock_session(resp)
    with patch("aiohttp.ClientSession", return_value=session), _patch_auth():
        adapter = _make_adapter()
        await adapter.run_job("job-name", {"K": "V"})

    body = session.post.call_args.kwargs["json"]
    assert "overrides" in body
    assert "containerOverrides" in body["overrides"]
    assert len(body["overrides"]["containerOverrides"]) == 1


# ============================================================================
# run_job — authentication header
# ============================================================================

async def test_run_job_sends_bearer_token():
    resp = _mock_resp()
    session = _mock_session(resp)
    with patch("aiohttp.ClientSession", return_value=session), _patch_auth("my-secret-token"):
        adapter = _make_adapter()
        await adapter.run_job("job-name", {})

    headers = session.post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer my-secret-token"


async def test_run_job_content_type_is_json():
    resp = _mock_resp()
    session = _mock_session(resp)
    with patch("aiohttp.ClientSession", return_value=session), _patch_auth():
        adapter = _make_adapter()
        await adapter.run_job("job-name", {})

    headers = session.post.call_args.kwargs["headers"]
    assert headers["Content-Type"] == "application/json"


# ============================================================================
# run_job — return value
# ============================================================================

async def test_run_job_returns_operation_name():
    resp = _mock_resp(json_data={"name": "operations/run-exec-456"})
    session = _mock_session(resp)
    with patch("aiohttp.ClientSession", return_value=session), _patch_auth():
        adapter = _make_adapter()
        result = await adapter.run_job("job-name", {})

    assert result == "operations/run-exec-456"


async def test_run_job_missing_name_field_returns_empty_string():
    resp = _mock_resp(json_data={})
    session = _mock_session(resp)
    with patch("aiohttp.ClientSession", return_value=session), _patch_auth():
        adapter = _make_adapter()
        result = await adapter.run_job("job-name", {})

    assert result == ""


# ============================================================================
# run_job — HTTP error handling
# ============================================================================

async def test_run_job_non_2xx_raises_runtime_error():
    resp = _mock_resp(ok=False, status=404, text="Job not found")
    session = _mock_session(resp)
    with patch("aiohttp.ClientSession", return_value=session), _patch_auth():
        adapter = _make_adapter()
        with pytest.raises(RuntimeError, match="run_job failed"):
            await adapter.run_job("job-name", {})


async def test_run_job_500_error_includes_status_in_message():
    resp = _mock_resp(ok=False, status=500, text="Internal error")
    session = _mock_session(resp)
    with patch("aiohttp.ClientSession", return_value=session), _patch_auth():
        adapter = _make_adapter()
        with pytest.raises(RuntimeError, match="500"):
            await adapter.run_job("job-name", {})


async def test_run_job_error_body_truncated_to_300_chars():
    long_error = "x" * 500
    resp = _mock_resp(ok=False, status=400, text=long_error)
    session = _mock_session(resp)
    with patch("aiohttp.ClientSession", return_value=session), _patch_auth():
        adapter = _make_adapter()
        with pytest.raises(RuntimeError) as exc_info:
            await adapter.run_job("job-name", {})

    # Body truncated — full 500-char string must NOT appear verbatim in message
    assert long_error not in str(exc_info.value)


async def test_run_job_2xx_does_not_raise():
    resp = _mock_resp(ok=True, status=200)
    session = _mock_session(resp)
    with patch("aiohttp.ClientSession", return_value=session), _patch_auth():
        adapter = _make_adapter()
        result = await adapter.run_job("job-name", {})  # should not raise

    assert isinstance(result, str)
