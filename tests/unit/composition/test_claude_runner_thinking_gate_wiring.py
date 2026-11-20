"""Both construction sites hand the deep-research runner ClaudeAdapter's thinking gate."""
import pytest

from tests.unit.composition.test_user_agent_factory_values import user_graph  # noqa: F401
from tests.support.prod_graph import USER_ID
from src.adapters.claude_adapter import ClaudeAdapter


async def test_the_job_runner_gets_the_adapter_gate(monkeypatch):
    import json
    from unittest.mock import AsyncMock, MagicMock, patch

    import job_main
    from src.domain.agent import AgentResponse, AgentStatus

    monkeypatch.setenv("JOB_QUERY", "q")
    monkeypatch.setenv("JOB_CONTEXT_JSON", json.dumps({"user_id": "u1", "account_id": "acc-1", "session_id": "u1:C0CHAN"}))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("GCS_MEDIA_BUCKET", raising=False)
    agent = MagicMock()
    agent.agent_id = "x"
    agent.execute = AsyncMock(return_value=AgentResponse(
        task_id="t", agent_id="x", status=AgentStatus.SUCCESS, confidence=1.0, result={"text": "report"}))
    with patch.object(job_main, "ClaudeDeepResearchRunnerAgent", return_value=agent) as runner_cls, \
         patch.object(job_main, "_build_task_queue", return_value=MagicMock()), \
         patch.object(job_main.anthropic, "AsyncAnthropic"), \
         patch.object(job_main, "_capture_research_result", AsyncMock()), \
         patch.object(job_main, "_record_billing", AsyncMock()), \
         patch.object(job_main, "deliver_deep_research", AsyncMock()):
        await job_main._run_job()

    assert runner_cls.call_args.kwargs["adaptive_thinking"] == ClaudeAdapter.takes_adaptive_thinking


@pytest.mark.slow
async def test_the_factory_runner_gets_the_adapter_gate(user_graph):  # noqa: F811
    runner = user_graph.app.coordinator.get_agent(f"claude_deep_research_runner_{USER_ID}")

    assert runner._adaptive_thinking == ClaudeAdapter.takes_adaptive_thinking
