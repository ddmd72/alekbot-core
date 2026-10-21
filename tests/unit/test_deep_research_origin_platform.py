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


# --- failure / cancellation events and event-carries-only-id ---------------

class TestWebhookFailureAndRetrieval:
    async def _post_event(self, event_type, data, fetch_response=None):
        notification = AsyncMock()
        app = Quart("t")
        app.register_blueprint(create_deep_research_webhooks_blueprint(
            notification_service=notification, webhook_secret=None,
            media_storage=MagicMock(), task_queue=AsyncMock(), fetch_response=fetch_response))
        with patch("src.web.deep_research_webhooks.deliver_deep_research",
                   new_callable=AsyncMock) as deliver:
            async with app.test_client() as c:
                resp = await c.post("/webhooks/openai/deep-research",
                                    data=json.dumps({"type": event_type, "data": data}),
                                    headers={"Content-Type": "application/json"})
        return resp, notification, deliver

    _MD = {"user_id": "u", "account_id": "a", "query": "q",
           "session_id": "u:C1", "origin_platform": "telegram"}

    @pytest.mark.parametrize("event", ["response.failed", "response.cancelled"])
    async def test_failure_and_cancel_notify_origin_channel(self, event):
        resp, notification, _ = await self._post_event(event, {"id": "r1", "metadata": self._MD})
        assert resp.status_code == 200
        kw = notification.notify.await_args.kwargs
        assert kw["channel_id_override"] == "C1"
        assert kw["platform_override"] == "telegram"

    async def test_event_with_only_id_retrieves_response(self):
        fetch = AsyncMock(return_value={"metadata": self._MD, "output_text": "report", "model": "m"})
        resp, _, deliver = await self._post_event("response.completed", {"id": "r1"}, fetch)
        assert resp.status_code == 200
        fetch.assert_awaited_once_with("r1")
        kw = deliver.call_args.kwargs
        assert kw["result_text"] == "report"
        assert kw["user_id"] == "u"
        assert kw["channel_id_override"] == "C1"
        assert kw["platform_override"] == "telegram"

    async def test_failed_event_with_only_id_notifies_origin_channel(self):
        fetch = AsyncMock(return_value={"metadata": self._MD})
        resp, notification, _ = await self._post_event("response.failed", {"id": "r1"}, fetch)
        assert resp.status_code == 200
        assert notification.notify.await_args.kwargs["user_id"] == "u"
        assert notification.notify.await_args.kwargs["channel_id_override"] == "C1"

    async def test_retrieval_failure_is_non_2xx_so_openai_redelivers(self):
        fetch = AsyncMock(side_effect=RuntimeError("boom"))
        resp, _, deliver = await self._post_event("response.completed", {"id": "r1"}, fetch)
        assert resp.status_code == 500
        deliver.assert_not_called()


class TestOpenAIFetchResponse:
    async def test_returns_metadata_and_output(self):
        adapter = OpenAIDeepResearchAdapter(api_key="k")
        adapter._client = AsyncMock()
        adapter._client.responses.retrieve.return_value = MagicMock(
            metadata={"user_id": "u"}, output_text="out", model="m")
        got = await adapter.fetch_response("r1")
        assert got == {"metadata": {"user_id": "u"}, "output_text": "out", "model": "m"}


# --- deliver_deep_research follow-on task context -------------------------

class TestDeliverCarriesOrigin:
    async def _context(self, **kw):
        from src.services.deep_research_delivery import deliver_deep_research
        queue = AsyncMock()
        await deliver_deep_research(result_text="r", user_id="u", account_id="a", query="q",
                                    task_queue=queue, session_id="u:C1", **kw)
        return queue.enqueue_agent_task.call_args.kwargs["context"]

    async def test_origin_channel_and_platform_in_html_task_context(self):
        ctx = await self._context(channel_id_override="C1", platform_override="telegram")
        assert ctx["origin_channel_id"] == "C1"
        assert ctx["origin_platform"] == "telegram"

    async def test_no_overrides_leaves_keys_out(self):
        ctx = await self._context()
        assert "origin_channel_id" not in ctx and "origin_platform" not in ctx
