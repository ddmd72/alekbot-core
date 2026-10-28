"""The [DriveAgent] log line names the model that ran, not the configured one (newcomer review F3).

The agent runs through `process()` (which opens the execution's billing scope) with its real `_call_llm`: the primary
provider is down, the declared fallback answers. `_call_llm` re-serves the turn on the fallback model and bills it
there; the log line must say so.
"""
import logging
from unittest.mock import AsyncMock, MagicMock

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.drive_agent import DriveAgent
from src.domain import drive_tool_texts as tt
from src.domain.agent import AgentConfig, AgentStatus
from src.domain.exceptions import LLMUnavailableError
from src.domain.llm import LLMResponse, ProviderCapabilities, ToolCall, UsageMetadata
from src.domain.user import PerformanceTier
from src.infrastructure.agent_config import DRIVE
from src.ports.llm_port import AgentExecutionContext, LLMPort
from tests.unit.agents.test_drive_agent import NOW, _msg, ops, prompts  # noqa: F401

PRIMARY, FALLBACK = "claude-sonnet-5-5", "gpt-6.1-sol"


def _report() -> LLMResponse:
    return LLMResponse(text=None, tool_calls=[ToolCall(name=tt.REPORT_TOOL, args={"note": "Searched for the lease."})],
                       usage_metadata=UsageMetadata(prompt_tokens=50, completion_tokens=10, total_tokens=60))


def _provider(answer=None, error=None) -> MagicMock:
    llm = MagicMock(spec=LLMPort)
    llm.generate_content = AsyncMock(side_effect=error) if error else AsyncMock(return_value=answer)
    return llm


def _agent(ops, prompts, primary, fallback):  # noqa: F811
    ctx = AgentExecutionContext(
        agent_type="drive", provider=primary, model_name=PRIMARY, tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities(), provider_name="claude",
        fallback_provider=fallback, fallback_model_name=FALLBACK, fallback_provider_name="openai",
        resilience_port=InMemoryProviderResilience())
    agent = DriveAgent(AgentConfig(agent_id="drive_agent_u1", agent_type="drive", timeout_ms=DRIVE.timeout_ms),
                       execution_context=ctx, operations=ops, prompt_builder=prompts, user_timezone="Europe/Madrid",
                       clock=lambda: NOW)
    agent.coordinator = MagicMock()
    return agent


def _log_line(caplog) -> str:
    return next(r.getMessage() for r in caplog.records if r.getMessage().startswith("[DriveAgent] user="))


async def test_a_fallback_run_logs_the_fallback_model(ops, prompts, caplog):  # noqa: F811
    primary = _provider(error=LLMUnavailableError("down", http_status=503))
    fallback = _provider(answer=_report())
    agent = _agent(ops, prompts, primary, fallback)

    with caplog.at_level(logging.INFO):
        resp = await agent.process(_msg())

    assert resp.status == AgentStatus.SUCCESS
    fallback.generate_content.assert_called_once()
    line = _log_line(caplog)
    assert f"model={FALLBACK} " in line
    assert PRIMARY not in line


async def test_a_run_the_primary_serves_logs_the_primary(ops, prompts, caplog):  # noqa: F811
    primary, fallback = _provider(answer=_report()), _provider(answer=_report())
    agent = _agent(ops, prompts, primary, fallback)

    with caplog.at_level(logging.INFO):
        await agent.process(_msg())

    fallback.generate_content.assert_not_called()
    assert f"model={PRIMARY} " in _log_line(caplog)
