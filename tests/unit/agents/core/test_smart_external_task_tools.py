"""Smart's external task tools end to end (EXTERNAL_TASK_QUEUE_RFC §5.3): the gate matrix, a chat create waiting for
confirmation, and what the persisted turn does not carry.

A real `SmartResponseAgent` (`smart_rig`) over a scripted model; the real `ExternalTaskService` over the fakes of
`tests/support/external_task_world.py`. Specialists answer through a stub coordinator. Alex Doe is `usr-1`.
"""
import pytest

from src.domain.agent import AgentResponse, AgentStatus
from src.domain.external_task import ExternalTaskStatus
from src.domain.external_task_receipt import EXTERNAL_TASK_RECEIPT_DELIVERY
from src.domain.user import UserBotConfig
from src.infrastructure.agent_coordinator import AgentCoordinator
from tests.support.external_task_world import TEXT, TITLE, ExternalTaskWorld
from tests.support.scripted_llm import ScriptedLLM, text, tools
from tests.support.tool_e2e import smart_message, smart_rig

S = ExternalTaskStatus
CREATE = ("create_external_task", {"worker": "research", "title": TITLE, "text": TEXT})
EXTERNAL_TOOLS = {"create_external_task", "list_external_tasks", "get_external_task", "cancel_external_task"}


class SpecialistStub(AgentCoordinator):
    """A registry-less coordinator whose specialists answer one plain string per intent."""

    def __init__(self) -> None:
        super().__init__()
        self.intents = []

    async def handle_delegation(self, intent, query, context, calling_agent_id):
        self.intents.append(intent)
        return AgentResponse.success(task_id="t", agent_id=intent, result=f"{intent}: plain text answer")


def declared(llm) -> set:
    return {t["name"] for t in llm.requests[0].tools} & EXTERNAL_TOOLS


async def run(world, script, *, flag=True, service=True, interactive=True, **context):
    llm = ScriptedLLM(script)
    agent = smart_rig(llm, user_id="usr-1", coordinator=SpecialistStub(),
                      user_config=UserBotConfig(external_tasks_enabled=flag),
                      external_tasks=world.service if service else None)
    message = smart_message("hand the irrigation comparison to research", interactive=interactive, user_id="usr-1",
                            account_id="acc-1", session_id="usr-1:C-garden", origin_platform="slack",
                            origin_channel_id="C-garden", **context)
    response = await agent.execute(message)
    assert response.status == AgentStatus.SUCCESS
    assert llm.unanswered == 0 and llm.pending() == 0
    return llm, response


@pytest.fixture
async def world():
    return await ExternalTaskWorld().seed()


# --- the gate ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("flag,service,interactive,labels,expected", [
    (True, True, True, ("research",), EXTERNAL_TOOLS),
    (True, True, True, (), EXTERNAL_TOOLS - {"create_external_task"}),     # an empty enum is not a valid schema
    (False, True, True, ("research",), set()),                              # the profile's flag is off
    (True, False, True, ("research",), set()),                              # no service (no task queue)
    (True, True, False, ("research",), set()),                              # a background run (E2)
])
async def test_the_gate_matrix(flag, service, interactive, labels, expected):
    world = await ExternalTaskWorld().seed(labels)
    llm, _ = await run(world, [text("ok")], flag=flag, service=service, interactive=interactive)
    assert declared(llm) == expected


async def test_when_the_service_refuses_the_worker_list_no_tool_is_offered(world):
    await world.flag("usr-1", False)       # the cached profile still says on; the service reads the flag fresh
    llm, _ = await run(world, [text("ok")], flag=True)
    assert declared(llm) == set()


async def test_when_the_worker_list_fails_no_tool_is_offered_and_the_turn_still_answers(world, monkeypatch):
    async def broken(user_id):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(world.workers, "list_workers", broken)
    llm, response = await run(world, [text("ok")])
    assert declared(llm) == set()
    assert response.status == AgentStatus.SUCCESS


async def test_the_worker_enum_is_this_executions_registered_labels():
    world = await ExternalTaskWorld().seed(("research", "calendar-watch"))
    llm, _ = await run(world, [text("ok")])
    create = next(t for t in llm.requests[0].tools if t["name"] == "create_external_task")
    assert sorted(create["parameters"]["properties"]["worker"]["enum"]) == ["calendar-watch", "research"]


# --- a chat create waits for confirmation --------------------------------------------------------------------------

async def test_a_create_in_a_clean_execution_waits_for_confirmation_and_the_owner_gets_the_full_text(world):
    llm, response = await run(world, [tools(CREATE), text("It waits for your confirmation.")])
    [task] = world.tasks.tasks.values()
    assert task.status is S.HELD and task.text == TEXT
    assert (task.origin.platform, task.origin.channel_id, task.origin.session_id) == ("slack", "C-garden",
                                                                                      "usr-1:C-garden")
    [item] = [i for i in response.delivery_items if i.type == EXTERNAL_TASK_RECEIPT_DELIVERY]
    assert item.data["text"] == TEXT and item.data["command"].startswith("$task send ")


async def test_a_create_after_a_memory_search_waits_for_confirmation_too(world):
    script = [tools(("delegate_to_specialist", {"intent": "search_memory", "query": "irrigation"})),
              tools(CREATE), text("ok")]
    await run(world, script)
    assert next(iter(world.tasks.tasks.values())).status is S.HELD


async def test_no_external_task_key_reaches_the_response_metadata(world):
    script = [tools(CREATE), tools(("list_external_tasks", {})), text("ok")]
    _, response = await run(world, script)
    task_id = next(iter(world.tasks.tasks))
    assert set(response.metadata) <= {"model", "tokens", "response_length", "response_summary",
                                      "response_summary_task"}  # no history_context key of any tool
    assert task_id not in str(response.metadata)


# --- review round: nested Smart, the workers tool -----------------------------

@pytest.mark.parametrize("context", [
    {"_call_chain": ["smart_response_agent_usr-1"]},                                   # reached through delegation
    {"_call_chain": ["lelik_agent", "alek_gateway_agent"], "interactive_delivery": True},   # the serialised send_to_chat shape
])
async def test_a_smart_reached_through_delegation_is_offered_no_task_tool(world, context):
    llm, _ = await run(world, [text("ok")], **context)
    assert declared(llm) == set() and "list_external_workers" not in {t["name"] for t in llm.requests[0].tools}


async def test_the_workers_tool_is_offered_with_the_others_even_with_no_worker():
    world = await ExternalTaskWorld().seed(())
    llm, _ = await run(world, [text("ok")])
    assert "list_external_workers" in {t["name"] for t in llm.requests[0].tools}


