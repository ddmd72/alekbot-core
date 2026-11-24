"""A final external task's alert through a real Smart (EXTERNAL_TASK_QUEUE_RFC §5.9; a system alert since 2026-11-24).

From the `external_task_deliver` runner through the real `UserNotificationService` and the real `AgentCoordinator` to a
real `SmartResponseAgent` over a scripted model, with the real `WorkspaceService` (GCS fake) and the real
`ExternalTaskService` of a flagged user. The model opens the result file the alert names; the turn is offered no queue
tool, because its origin (`background:external_task_deliver`) is not a chat turn: a worker's result never starts new
work. Alex Doe is `usr-1`; the worker is `research`.
"""
from unittest.mock import AsyncMock

import pytest

from src.domain.user import UserBotConfig
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.notification_sla import NOTIFICATION_SLA
from src.services.inactive_user_check import InactiveUserCheck
from src.services.user_notification_service import UserNotificationService
from tests.support.scripted_llm import ScriptedLLM, text, tool_results, tools
from tests.support.tool_e2e import smart_rig
from tests.unit.services.test_external_task_alert_delivery import RESULT, World, path_of

QUEUE_TOOLS = {"create_external_task", "list_external_tasks", "get_external_task", "cancel_external_task",
               "list_external_workers"}
ANSWER = "research finished: Kit A costs 40 USD and Kit B 55 USD."


@pytest.fixture
async def world() -> World:
    return await World().seed()


async def test_the_alert_reaches_smart_which_reads_the_result_file_and_is_offered_no_queue_tool(world):
    task_id = await world.done()
    path = path_of(task_id)
    llm = ScriptedLLM([tools(("workspace", {"command": "view", "path": f"workspace:{path}"})), text(ANSWER)])
    coordinator = AgentCoordinator()
    smart = smart_rig(llm, user_id="usr-1", coordinator=coordinator, workspace_service=world.workspace,
                      user_config=UserBotConfig(external_tasks_enabled=True))
    coordinator.register_agent(smart)
    world.notifications = UserNotificationService(
        state_repo=AsyncMock(), channel_factory=world.factory, coordinator=coordinator,
        notification_sla=NOTIFICATION_SLA, session_store=world.sessions, inactive_users=InactiveUserCheck(world.users))

    assert await world.deliverer(world.workspace)(world.jobs("external_task_deliver")[-1], {}) == (
        {"status": "delivered"}, 200)

    assert llm.unanswered == 0 and llm.pending() == 0
    offered = {t["name"] for t in llm.requests[0].tools}
    assert "workspace" in offered and not offered & QUEUE_TOOLS
    turn = " ".join(p.text or "" for p in llm.requests[0].messages[-1].parts)
    assert "[System: An external task has ended." in turn and f"workspace:{path}" in turn and "Kit A" not in turn
    [(name, viewed)] = tool_results(llm)
    assert name == "workspace" and RESULT in str(viewed)
    assert world.channel.posts == [ANSWER]
    assert world.stored(task_id).delivery.path == path
