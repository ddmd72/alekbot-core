"""
Unit tests for HelpAgent.

Coverage:
  can_handle() — True for QUERY, other intents
  execute()    — returns success with CAPABILITIES_TEXT
"""
import pytest
from unittest.mock import MagicMock

from src.agents.help_agent import HelpAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentIntent, AgentStatus
from src.utils.capabilities import CAPABILITIES_TEXT


def _make_config():
    return AgentConfig(agent_id="help_agent_u1", agent_type="help")


def _make_message(intent=AgentIntent.QUERY, task_id="t1"):
    msg = MagicMock(spec=AgentMessage)
    msg.task_id = task_id
    msg.intent = intent
    msg.sender = "coordinator"
    msg.recipient = "help_agent_u1"
    msg.payload = {}
    msg.context = {"user_id": "u1"}
    return msg


class TestHelpAgent:

    async def test_can_handle_query_intent(self):
        agent = HelpAgent(_make_config())
        assert await agent.can_handle(_make_message(AgentIntent.QUERY)) is True

    async def test_can_handle_delegate_intent(self):
        agent = HelpAgent(_make_config())
        assert await agent.can_handle(_make_message(AgentIntent.DELEGATE)) is False

    async def test_execute_returns_success(self):
        agent = HelpAgent(_make_config())
        response = await agent.execute(_make_message())
        assert response.status == AgentStatus.SUCCESS

    async def test_execute_result_is_capabilities_text(self):
        agent = HelpAgent(_make_config())
        response = await agent.execute(_make_message())
        assert response.result == CAPABILITIES_TEXT

    async def test_execute_confidence_is_one(self):
        agent = HelpAgent(_make_config())
        response = await agent.execute(_make_message())
        assert response.confidence == 1.0

    async def test_execute_task_id_preserved(self):
        agent = HelpAgent(_make_config())
        response = await agent.execute(_make_message(task_id="task-xyz"))
        assert response.task_id == "task-xyz"
