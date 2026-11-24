"""The reminders specialist learns the taint of the execution, and a chat message written during a long turn taints it
(EXTERNAL_TASK_QUEUE_RFC §6.1).

A real `SmartResponseAgent` over a scripted model and the external task world. Alex Doe is `usr-1`.
"""
import time

import pytest

from src.domain.agent import AgentResponse, AgentStatus
from src.domain.execution_taint import EXECUTION_TAINTED_KEY
from src.domain.external_task import ExternalTaskStatus
from src.domain.llm import Message, MessagePart
from src.domain.turn_clock import CURRENT_TURN_CLOCK, TurnClock
from src.domain.user import UserBotConfig
from src.infrastructure.agent_coordinator import AgentCoordinator
from tests.support.external_task_world import TEXT, TITLE, ExternalTaskWorld
from tests.support.scripted_llm import ScriptedLLM, text, tools
from tests.support.tool_e2e import smart_message, smart_rig

S = ExternalTaskStatus
CREATE = ("create_external_task", {"worker": "research", "title": TITLE, "text": TEXT})
LABEL = "compare irrigation kits for Shelbyville"
PLAIN = {"note_id": "1729401234568", "text": LABEL, "untrusted": False}


class Specialists(AgentCoordinator):
    """Answers every delegation with plain text and records the context each one was dispatched with."""

    def __init__(self) -> None:
        super().__init__()
        self.contexts = []

    async def handle_delegation(self, intent, query, context, calling_agent_id):
        self.contexts.append((intent, dict(context)))
        return AgentResponse.success(task_id="t", agent_id=intent, result=f"{intent}: plain text answer")


async def run(world, script, notes, coordinator=None):
    llm = ScriptedLLM(script)
    agent = smart_rig(llm, user_id="usr-1", coordinator=coordinator or Specialists(),
                      user_config=UserBotConfig(external_tasks_enabled=True), external_tasks=world.service)
    message = smart_message("hand the irrigation comparison to research", user_id="usr-1", account_id="acc-1",
                            session_id="usr-1:C-garden", origin_platform="slack", origin_channel_id="C-garden",
                            agent_notes=notes)
    response = await agent.execute(message)
    assert response.status == AgentStatus.SUCCESS and llm.unanswered == 0 and llm.pending() == 0
    return response


@pytest.fixture
async def world():
    return await ExternalTaskWorld().seed()


async def test_an_unflagged_note_leaves_the_create_queued(world):
    response = await run(world, [tools(CREATE), text("ok")], [PLAIN])
    assert next(iter(world.tasks.tasks.values())).status is S.QUEUED
    assert response.metadata[EXECUTION_TAINTED_KEY] is False


async def test_the_reminders_specialist_learns_the_taint_of_the_execution(world):
    reminder = ("delegate_to_specialist", {"intent": "manage_self_reminders", "query": "remind me on Friday"})
    web = ("delegate_to_specialist", {"intent": "search_web", "query": "irrigation kits"})
    clean, tainted = Specialists(), Specialists()
    await run(world, [tools(reminder), text("ok")], [], clean)
    await run(world, [tools(web), tools(reminder), text("ok")], [], tainted)
    assert [c.get(EXECUTION_TAINTED_KEY) for i, c in clean.contexts if i == "manage_self_reminders"] == [False]
    assert [c.get(EXECUTION_TAINTED_KEY) for i, c in tainted.contexts if i == "manage_self_reminders"] == [True]


async def test_a_flagged_chat_message_written_during_a_long_turn_holds_a_create(world):
    """The owner's chat since the run's snapshot is read before the next call; when it carries outside content the
    execution is tainted from then on, so the create that follows is held."""
    late = Message(role="user", parts=[MessagePart(text="also compare drip lines for Shelbyville")],
                   created_at=time.time() + 1000, execution_tainted=True)

    async def fetch_since(since):
        return [late]

    clock = TurnClock.start(fetch_since=fetch_since)
    clock.marked = True
    token = CURRENT_TURN_CLOCK.set(clock)
    try:
        response = await run(world, [tools(CREATE), text("ok")], [])
    finally:
        CURRENT_TURN_CLOCK.reset(token)
    assert next(iter(world.tasks.tasks.values())).status is S.HELD
    assert response.metadata[EXECUTION_TAINTED_KEY] is True
