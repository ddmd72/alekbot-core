from unittest.mock import patch

from src.domain.agent import AgentStatus
from src.domain.sandbox_errors import SandboxMisconfigured, SandboxUnavailable
from src.ports.docx_runner_port import DocxRunnerError
from tests.unit.agents.test_doc_generator_agent import (  # noqa: F401  (fixtures)
    _FAKE_DOCX_BYTES,
    _make_message,
    agent,
    mock_llm,
    mock_prompt_builder,
    mock_runner,
)


async def test_doc_generator_stops_on_unavailable_without_retrying_the_model(agent, mock_runner, mock_llm):
    mock_runner.run.side_effect = SandboxUnavailable("down")
    agent._on_agent_error = AsyncMock() if False else agent._on_agent_error
    with patch.object(agent, "_on_agent_error") as on_error:
        response = await agent.execute(_make_message())
    on_error.assert_called_once()
    assert response.status == AgentStatus.FAILED
    assert mock_llm.generate_content.await_count == 1
    assert mock_runner.run.await_count == 1


async def test_doc_generator_stops_on_misconfigured(agent, mock_runner, mock_llm):
    mock_runner.run.side_effect = SandboxMisconfigured("403")
    with patch.object(agent, "_on_agent_error") as on_error:
        response = await agent.execute(_make_message())
    on_error.assert_called_once()
    assert response.status == AgentStatus.FAILED
    assert mock_llm.generate_content.await_count == 1


async def test_doc_generator_still_repairs_on_script_error(agent, mock_runner, mock_llm):
    mock_runner.run.side_effect = [DocxRunnerError("SyntaxError"), _FAKE_DOCX_BYTES]
    response = await agent.execute(_make_message())
    assert response.status == AgentStatus.SUCCESS
    assert mock_llm.generate_content.await_count == 2


async def test_doc_generator_repairs_on_sandbox_lost(agent, mock_runner, mock_llm):
    mock_runner.run.side_effect = [DocxRunnerError("sandbox_lost: the run ended without a result twice"),
                                   _FAKE_DOCX_BYTES]
    response = await agent.execute(_make_message())
    assert response.status == AgentStatus.SUCCESS
    assert mock_llm.generate_content.await_count == 2


async def test_sandbox_failure_after_a_captured_docx_keeps_the_docx(agent, mock_runner, mock_llm):
    from src.domain.llm import LLMResponse, ToolCall
    two_calls = LLMResponse(text=None, tool_calls=[ToolCall(name="generate_docx", args={"js_code": "a"}),
                                                   ToolCall(name="generate_docx", args={"js_code": "b"})])
    mock_llm.generate_content.return_value = two_calls
    mock_runner.run.side_effect = [_FAKE_DOCX_BYTES, SandboxUnavailable("down")]
    response = await agent.execute(_make_message())
    assert response.status == AgentStatus.SUCCESS
