"""The drive specialist's prompt is built without the user's location (it never needs it)."""
from unittest.mock import AsyncMock, MagicMock

from src.agents.drive_agent import DriveAgent
from src.domain import drive_tool_texts as tt
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage
from src.domain.drive_line import DriveLine
from src.domain.drive_preflight import DrivePreflight
from src.domain.drive_search_line import SearchLine
from src.infrastructure.agent_config import DRIVE
from src.infrastructure.drive_operations import DriveOperations
from src.ports.llm_port import AgentExecutionContext
from tests.support.scripted_llm import calls, script


async def test_the_specialist_asks_the_builder_to_leave_the_location_out():
    ops = AsyncMock(spec=DriveOperations)
    ops.preflight.return_value = DrivePreflight(DriveLine.CONNECTED, SearchLine.ON)
    prompts = AsyncMock()
    prompts.build_for_agent.return_value = "DriveAgent extends Agent { }"
    ctx = MagicMock(spec=AgentExecutionContext)
    ctx.provider, ctx.model_name = AsyncMock(), "claude-sonnet-5-5"
    agent = DriveAgent(AgentConfig(agent_id="drive_agent_u1", agent_type="drive", timeout_ms=DRIVE.timeout_ms),
                       execution_context=ctx, operations=ops, prompt_builder=prompts, user_timezone="Europe/Madrid")
    agent.coordinator = MagicMock()
    agent._call_llm, _ = script(calls((tt.REPORT_TOOL, {"note": "Searched."})))
    msg = AgentMessage.create(sender="coordinator", recipient="drive_agent_u1", intent=AgentIntent.QUERY,
                              payload={"query": "find my lease", "intent": "work_with_drive_files"},
                              context={"user_id": "u1", "account_id": "a1", "interactive_delivery": True})
    await agent.execute(msg)
    assert prompts.build_for_agent.await_args.kwargs["include_location"] is False
