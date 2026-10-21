"""Deep research reports return to the platform+channel the request came from (S1b task E).

Covers the agent -> port hand-off, both live adapters (Claude job context, OpenAI metadata),
job_main and the OpenAI webhook; context/metadata without the field behave as before.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from quart import Quart

import job_main
from src.adapters.claude_deep_research_adapter import ClaudeDeepResearchAdapter
from src.adapters.openai_deep_research_adapter import OpenAIDeepResearchAdapter
from src.agents.deep_research_agent import DeepResearchAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse, AgentStatus
from src.domain.user import PerformanceTier
from src.ports.deep_research_port import DeepResearchPort
from src.ports.job_runner_port import JobRunnerPort
from src.web.deep_research_webhooks import create_deep_research_webhooks_blueprint


# --- agent -> port ---------------------------------------------------------

def _agent_and_port():
    port = AsyncMock(spec=DeepResearchPort)
    port.create_interaction.return_value = "job-1"
    agent = DeepResearchAgent(
        config=AgentConfig(agent_id="dr", agent_type="deep_research", timeout_ms=60_000,
                           capabilities=["deep_research"]),
        job_port=port, tier=PerformanceTier.BALANCED, prompt_builder=None, user_id="u1",
    )
    return agent, port


def _msg(context):
    return AgentMessage(
        task_id="t", sender="smart", recipient="dr", intent=AgentIntent.QUERY,
        payload={"query": "q", "brief": "q", "language": "English"}, context=context,
    )


class TestAgentPassesOriginPlatform:
    async def test_origin_platform_from_context_reaches_port(self):
        agent, port = _agent_and_port()
        await agent.execute(_msg({"user_id": "u1", "account_id": "a1",
                                  "session_id": "u1:C1", "origin_platform": "telegram"}))
        assert port.create_interaction.await_args.kwargs["origin_platform"] == "telegram"

    async def test_missing_origin_platform_is_none(self):
        agent, port = _agent_and_port()
        await agent.execute(_msg({"user_id": "u1", "account_id": "a1"}))
        assert port.create_interaction.await_args.kwargs["origin_platform"] is None


# --- Claude adapter --------------------------------------------------------

class TestClaudeAdapterContext:
    async def _ctx(self, **kw):
        runner = AsyncMock(spec=JobRunnerPort)
        adapter = ClaudeDeepResearchAdapter(job_runner=runner, job_name="j")
        await adapter.create_interaction(
            query="q", user_id="u", account_id="a", original_query="o", session_id="u:C1", **kw)
        return json.loads(runner.run_job.call_args.kwargs["env_overrides"]["JOB_CONTEXT_JSON"])

    async def test_origin_platform_in_job_context(self):
        assert (await self._ctx(origin_platform="slack"))["origin_platform"] == "slack"

    async def test_absent_platform_is_empty_string(self):
        assert (await self._ctx())["origin_platform"] == ""


# --- job_main --------------------------------------------------------------

async def _run_job_with(monkeypatch, context):
    monkeypatch.setenv("JOB_QUERY", "q")
    monkeypatch.setenv("JOB_CONTEXT_JSON", json.dumps(context))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("GCS_MEDIA_BUCKET", raising=False)
    agent = MagicMock()
    agent.agent_id = "x"
    agent.execute = AsyncMock(return_value=AgentResponse(
        task_id="t", agent_id="x", status=AgentStatus.SUCCESS, confidence=1.0, result={"text": "report"}))
    with patch.object(job_main, "ClaudeDeepResearchRunnerAgent", return_value=agent), \
         patch.object(job_main, "_build_task_queue", return_value=MagicMock()), \
         patch.object(job_main.anthropic, "AsyncAnthropic"), \
         patch.object(job_main, "_capture_research_result", AsyncMock()), \
         patch.object(job_main, "_record_billing", AsyncMock()), \
         patch.object(job_main, "deliver_deep_research", AsyncMock()) as deliver:
        await job_main._run_job()
    return deliver.await_args.kwargs


class TestJobMainOverrides:
    async def test_passes_channel_and_platform(self, monkeypatch):
        kw = await _run_job_with(monkeypatch, {
            "user_id": "u", "account_id": "a", "session_id": "u:C1", "origin_platform": "telegram"})
        assert kw["channel_id_override"] == "C1"
        assert kw["platform_override"] == "telegram"

    async def test_old_context_has_no_override_and_does_not_crash(self, monkeypatch):
        kw = await _run_job_with(monkeypatch, {"user_id": "u", "account_id": "a"})
        assert kw["channel_id_override"] is None
        assert kw["platform_override"] is None


# --- OpenAI adapter + webhook ---------------------------------------------

class TestOpenAIMetadata:
    async def _metadata(self, **kw):
        adapter = OpenAIDeepResearchAdapter(api_key="k")
        adapter._client = AsyncMock()
        adapter._client.responses.create.return_value = MagicMock(id="resp-123456789012345678")
        await adapter.create_interaction(
            query="q", user_id="u", account_id="a", original_query="o", session_id="u:C1", **kw)
        return adapter._client.responses.create.call_args.kwargs["metadata"]

    async def test_metadata_carries_platform(self):
        md = await self._metadata(origin_platform="telegram")
        assert md["origin_platform"] == "telegram"
        assert len(md) <= 16 and all(isinstance(v, str) and len(v) <= 512 for v in md.values())

    async def test_metadata_without_platform_is_empty_string(self):
        assert (await self._metadata())["origin_platform"] == ""


class TestWebhookPlatform:
    async def _post(self, metadata):
        app = Quart("t")
        app.register_blueprint(create_deep_research_webhooks_blueprint(
            notification_service=AsyncMock(), webhook_secret=None,
            media_storage=MagicMock(), task_queue=AsyncMock()))
        payload = {"type": "response.completed",
                   "data": {"id": "resp_1", "output_text": "text", "metadata": metadata}}
        with patch("src.web.deep_research_webhooks.deliver_deep_research",
                   new_callable=AsyncMock) as deliver:
            async with app.test_client() as c:
                resp = await c.post("/webhooks/openai/deep-research", data=json.dumps(payload),
                                    headers={"Content-Type": "application/json"})
            assert resp.status_code == 200
            return deliver.call_args.kwargs

    async def test_platform_override_from_metadata(self):
        kw = await self._post({"user_id": "u", "account_id": "a", "query": "q",
                               "session_id": "u:C1", "origin_platform": "telegram"})
        assert kw["platform_override"] == "telegram"
        assert kw["channel_id_override"] == "C1"

    @pytest.mark.parametrize("md_extra", [{}, {"origin_platform": ""}])
    async def test_missing_platform_is_no_override(self, md_extra):
        kw = await self._post({"user_id": "u", "account_id": "a", "query": "q",
                               "session_id": "u:C1", **md_extra})
        assert kw["platform_override"] is None
