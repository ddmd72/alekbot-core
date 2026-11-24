"""Smart's external task tools end to end (EXTERNAL_TASK_QUEUE_RFC §5.3): the gate matrix, the taint rule through the
real delegation engine, and what the persisted turn does not carry.

A real `SmartResponseAgent` (`smart_rig`) over a scripted model; the real `ExternalTaskService` over the fakes of
`tests/support/external_task_world.py`. Specialists answer through a stub coordinator. Alex Doe is `usr-1`.
"""
import pytest

from src.domain.external_task_entry import TaskEntry
from src.domain.agent import AgentResponse, AgentStatus
from src.domain.external_task import ExternalTaskStatus
from src.domain.external_task_receipt import EXTERNAL_TASK_RECEIPT_DELIVERY
from src.domain.llm import Message, MessagePart
from src.domain.user import UserBotConfig
from src.infrastructure.agent_coordinator import AgentCoordinator
from tests.support.external_task_world import ORIGIN, TEXT, TITLE, ExternalTaskWorld
from tests.support.scripted_llm import ScriptedLLM, text, tool_results, tools
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


# --- the taint rule through the engine --------------------------------------------------------------------------

async def test_a_create_in_a_clean_execution_is_queued_and_the_owner_gets_the_full_text(world):
    llm, response = await run(world, [tools(CREATE), text("Handed over.")])
    [task] = world.tasks.tasks.values()
    assert task.status is S.QUEUED and task.text == TEXT
    assert (task.origin.platform, task.origin.channel_id, task.origin.session_id) == ("slack", "C-garden",
                                                                                      "usr-1:C-garden")
    [item] = [i for i in response.delivery_items if i.type == EXTERNAL_TASK_RECEIPT_DELIVERY]
    assert item.data["text"] == TEXT and item.data["command"] is None


async def test_after_a_web_search_result_the_create_is_held(world):
    script = [tools(("delegate_to_specialist", {"intent": "search_web", "query": "irrigation kits"})),
              tools(CREATE), text("Waiting for your confirmation.")]
    llm, response = await run(world, script)
    [task] = world.tasks.tasks.values()
    assert task.status is S.HELD
    [item] = [i for i in response.delivery_items if i.type == EXTERNAL_TASK_RECEIPT_DELIVERY]
    code = item.data["command"].split()[-1]
    assert all(code not in str(result) for _, result in tool_results(llm))     # the model never reads the code


async def test_after_a_memory_search_the_create_is_queued(world):
    script = [tools(("delegate_to_specialist", {"intent": "search_memory", "query": "garden"})),
              tools(CREATE), text("Handed over.")]
    await run(world, script)
    assert next(iter(world.tasks.tasks.values())).status is S.QUEUED


async def test_a_chat_upload_in_the_request_holds_the_create(world):
    upload = MessagePart(file_data={"uri": "gs://bucket/plan.pdf", "mime_type": "application/pdf"})
    await run(world, [tools(CREATE), text("ok")], current_message_parts=[MessagePart(text="see plan"), upload])
    assert next(iter(world.tasks.tasks.values())).status is S.HELD


async def test_reading_a_task_holds_a_create_later_in_the_same_execution(world):
    first = await world.service.create("usr-1", "research", "earlier", "an earlier brief", ORIGIN, entry=TaskEntry.REMINDER)
    script = [tools(("get_external_task", {"task_id": first.task_id})), tools(CREATE), text("ok")]
    await run(world, script)
    created = [t for t in world.tasks.tasks.values() if t.task_id != first.task_id]
    assert [t.status for t in created] == [S.HELD]


async def test_no_external_task_key_reaches_the_response_metadata(world):
    script = [tools(CREATE), tools(("list_external_tasks", {})), text("ok")]
    _, response = await run(world, script)
    task_id = next(iter(world.tasks.tasks))
    assert set(response.metadata) <= {"model", "tokens", "response_length", "response_summary",
                                      "response_summary_task", "execution_tainted"}  # no history_context key of any tool
    assert task_id not in str(response.metadata)


# --- review round: nested Smart, taint across turns, forwarded text, the workers tool -----------------------------

class HistorySessions:
    """A session store that returns a fixed history (Smart reads only `load_session(...).history`)."""

    def __init__(self, history) -> None:
        self.history = history

    async def load_session(self, session_id):
        return type("Session", (), {"history": self.history})()


async def run_with_history(world, script, history, **context):
    llm = ScriptedLLM(script)
    agent = smart_rig(llm, user_id="usr-1", coordinator=SpecialistStub(), session_store=HistorySessions(history),
                      user_config=UserBotConfig(external_tasks_enabled=True), external_tasks=world.service)
    message = smart_message("hand it to research", user_id="usr-1", account_id="acc-1", session_id="usr-1:C-garden",
                            origin_platform="slack", origin_channel_id="C-garden", **context)
    response = await agent.execute(message)
    assert response.status == AgentStatus.SUCCESS
    return llm, response


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


async def test_the_response_says_whether_the_execution_was_tainted(world):
    _, clean = await run(world, [tools(CREATE), text("ok")])
    _, dirty = await run(world, [tools(("delegate_to_specialist", {"intent": "search_web", "query": "q"})),
                                 text("ok")])
    assert clean.metadata["execution_tainted"] is False and dirty.metadata["execution_tainted"] is True


def _history(model_flag=None):
    user = Message(role="user", parts=[MessagePart(text="earlier question")])
    kwargs = {} if model_flag is None else {"execution_tainted": model_flag}
    return [user, Message(role="model", parts=[MessagePart(text="earlier answer")], **kwargs)]


async def test_a_flagged_model_message_in_the_history_holds_the_create(world):
    await run_with_history(world, [tools(CREATE), text("ok")], _history(True))
    assert next(iter(world.tasks.tasks.values())).status is S.HELD


@pytest.mark.parametrize("flag", [None, False])
async def test_an_old_or_clean_model_message_does_not_taint_by_itself(world, flag):
    await run_with_history(world, [tools(CREATE), text("ok")], _history(flag))
    assert next(iter(world.tasks.tasks.values())).status is S.QUEUED


async def test_a_turn_after_a_web_search_turn_is_held(world):
    _, first = await run(world, [tools(("delegate_to_specialist", {"intent": "search_web", "query": "q"})),
                                 text("found kits")])
    saved = Message(role="model", parts=[MessagePart(text="found kits")],
                    execution_tainted=first.metadata["execution_tainted"])
    await run_with_history(world, [tools(CREATE), text("ok")], [saved])
    assert next(iter(world.tasks.tasks.values())).status is S.HELD


async def test_a_forwarded_message_holds_the_create(world):
    await run(world, [tools(CREATE), text("ok")], metadata={"forwarded": True})
    assert next(iter(world.tasks.tasks.values())).status is S.HELD


async def test_a_list_that_includes_a_task_created_tainted_holds_a_later_create(world):
    await world.service.create("usr-1", "research", "from a web page", "brief", ORIGIN, entry=TaskEntry.CHAT)
    await run(world, [tools(("list_external_tasks", {})), tools(CREATE), text("ok")])
    created = [t for t in world.tasks.tasks.values() if t.title == TITLE]
    assert [t.status for t in created] == [S.HELD]
