"""Deep research reports return to the platform+channel the request came from (S1b task E).

Covers the agent -> port hand-off, the Claude adapter's job context, job_main and the report task
deliver_deep_research enqueues; a context without the field behaves as before.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import job_main
from src.adapters.claude_deep_research_adapter import ClaudeDeepResearchAdapter
from src.agents.deep_research_agent import DeepResearchAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse, AgentStatus
from src.domain.user import PerformanceTier
from src.ports.deep_research_port import DeepResearchPort
from src.ports.job_runner_port import JobRunnerPort


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


class TestEnqueueFailureNotice:
    async def _run(self, queue):
        from src.services.deep_research_delivery import deliver_deep_research
        notifier = AsyncMock()
        await deliver_deep_research(
            result_text="r", user_id="u", account_id="a", query="q", task_queue=queue,
            session_id="u:C1", channel_id_override="C1", platform_override="telegram",
            notification=notifier)
        return notifier

    async def test_enqueue_failure_notifies_once_with_overrides(self):
        queue = AsyncMock()
        queue.enqueue_agent_task.side_effect = RuntimeError("tasks down")
        notifier = await self._run(queue)
        notifier.notify.assert_awaited_once()
        kw = notifier.notify.await_args.kwargs
        assert kw["channel_id_override"] == "C1" and kw["platform_override"] == "telegram"
        assert "tasks down" not in kw["system_alert"]

    async def test_enqueue_ok_does_not_notify(self):
        notifier = await self._run(AsyncMock())
        notifier.notify.assert_not_called()

    async def test_notice_failure_is_not_raised(self):
        queue = AsyncMock()
        queue.enqueue_agent_task.side_effect = RuntimeError("tasks down")
        from src.services.deep_research_delivery import deliver_deep_research
        notifier = AsyncMock()
        notifier.notify.side_effect = RuntimeError("slack down")
        await deliver_deep_research(result_text="r", user_id="u", account_id="a", query="q",
                                    task_queue=queue, notification=notifier)
