"""I4 end to end: the model calls a write tool with `text_item t1` when the commission has no `file_contents`; the code's
refusal reaches the orchestrator as the NOT_ATTACHED instruction (DRIVE_SPECIALIST_RFC §7.2, §8.1)."""
from unittest.mock import AsyncMock, MagicMock

from src.agents.drive_agent import DriveAgent
from src.domain import drive_answer_texts as t
from src.domain import drive_tool_texts as tt
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.drive_line import DriveLine
from src.domain.drive_preflight import DrivePreflight
from src.domain.drive_search_line import SearchLine
from src.infrastructure.agent_config import DRIVE
from src.infrastructure.drive_operations import DriveOperations
from src.ports.llm_port import AgentExecutionContext
from tests.support.scripted_llm import calls, script


async def test_save_without_an_item_tells_the_orchestrator_how_to_attach_the_text():
    ops = AsyncMock(spec=DriveOperations)
    ops.preflight.return_value = DrivePreflight(DriveLine.CONNECTED, SearchLine.ON)
    prompts = AsyncMock()
    prompts.build_for_agent.return_value = "prompt"
    ctx = MagicMock(spec=AgentExecutionContext)
    ctx.provider, ctx.model_name = AsyncMock(), "claude-sonnet-5-5"
    agent = DriveAgent(AgentConfig(agent_id="drive_agent_u1", agent_type="drive", timeout_ms=DRIVE.timeout_ms),
                       execution_context=ctx, operations=ops, prompt_builder=prompts)
    agent.coordinator = MagicMock()
    agent._call_llm, _ = script(calls((tt.SAVE_TEXT_FILE, {"text_item": "t1"})),
                                calls((tt.REPORT_TOOL, {"note": "No text was attached."})))
    message = AgentMessage.create(sender="s", recipient="drive_agent_u1", intent=AgentIntent.QUERY,
                                  payload={"query": "save this note: buy milk"},
                                  context={"user_id": "u1", "account_id": "a1"})
    resp = await agent.execute(message)
    ops.save_text.assert_not_called()
    assert resp.status == AgentStatus.SUCCESS and resp.metadata["drive_status"] == "not_possible"
    assert f"What to do: {t.NOT_ATTACHED}" in resp.result
