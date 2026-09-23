import pytest
from unittest.mock import AsyncMock

from quart import Quart

from src.domain.request_context import get_current_account_id
from src.web.voice_control_plane_app import create_voice_control_plane_blueprint


def _app(provider, oidc=True, prompt_content_store=None):
    app = Quart(__name__)
    app.register_blueprint(create_voice_control_plane_blueprint(
        ephemeral_store=AsyncMock(), quota_service=AsyncMock(),
        prompt_content_store=prompt_content_store if prompt_content_store is not None else AsyncMock(),
        summary_consumer=AsyncMock(), oidc_verifier=AsyncMock(return_value=oidc), alert_sink=AsyncMock(),
        lelik_agent_provider=provider,
    ))
    return app.test_client()


_BODY = {"user_id": "u1", "account_id": "a1",
         "arguments": {"intent": "search_web", "query": "q"}, "call_context": []}


@pytest.mark.asyncio
async def test_delegate_runs_lelik_dispatch_inside_the_callers_request_context():
    seen = {}
    agent = AsyncMock()

    async def delegate(**kwargs):
        seen.update(kwargs, account=get_current_account_id())
        return "sunny"

    agent.delegate.side_effect = delegate
    provider = AsyncMock(return_value=agent)
    response = await _app(provider).post("/voice/delegate", json=_BODY, headers={"Authorization": "Bearer x"})
    assert response.status_code == 200
    assert (await response.get_json()) == {"output": "sunny"}
    provider.assert_awaited_once_with("u1")
    assert seen["arguments"] == {"intent": "search_web", "query": "q"}
    assert seen["account"] == "a1"


@pytest.mark.asyncio
async def test_delegate_requires_oidc():
    response = await _app(AsyncMock(), oidc=False).post("/voice/delegate", json=_BODY)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_delegate_503_when_lelik_is_not_configured():
    response = await _app(AsyncMock(return_value=None)).post(
        "/voice/delegate", json=_BODY, headers={"Authorization": "Bearer x"})
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_delegate_500_when_dispatch_raises():
    agent = AsyncMock()
    agent.delegate.side_effect = RuntimeError("boom")
    response = await _app(AsyncMock(return_value=agent)).post(
        "/voice/delegate", json=_BODY, headers={"Authorization": "Bearer x"})
    assert response.status_code == 500


# =============================================================================
# OWNER ADDITION (delta 9) — prompt content flush before returning
# =============================================================================


@pytest.mark.asyncio
async def test_delegate_flushes_prompt_content_before_returning():
    prompt_content_store = AsyncMock()
    agent = AsyncMock()
    agent.delegate.return_value = "sunny"
    provider = AsyncMock(return_value=agent)
    response = await _app(provider, prompt_content_store=prompt_content_store).post(
        "/voice/delegate", json=_BODY, headers={"Authorization": "Bearer x"})
    assert response.status_code == 200
    prompt_content_store.flush.assert_awaited_once()

    prompt_content_store = AsyncMock()
    agent = AsyncMock()
    agent.delegate.side_effect = RuntimeError("boom")
    provider = AsyncMock(return_value=agent)
    response = await _app(provider, prompt_content_store=prompt_content_store).post(
        "/voice/delegate", json=_BODY, headers={"Authorization": "Bearer x"})
    assert response.status_code == 500
    prompt_content_store.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_delegate_flush_failure_does_not_change_the_response():
    prompt_content_store = AsyncMock()
    prompt_content_store.flush.side_effect = RuntimeError("flush boom")
    agent = AsyncMock()
    agent.delegate.return_value = "sunny"
    provider = AsyncMock(return_value=agent)
    response = await _app(provider, prompt_content_store=prompt_content_store).post(
        "/voice/delegate", json=_BODY, headers={"Authorization": "Bearer x"})
    assert response.status_code == 200
    assert (await response.get_json()) == {"output": "sunny"}
