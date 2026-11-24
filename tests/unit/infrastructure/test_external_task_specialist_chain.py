"""The chain (EXTERNAL_TASK_QUEUE_RFC §5.3): an owner's chat turn through Smart's engine to the coordinator, which builds
the task specialist lazily through the factory's own `create_agent_on_demand`; every chat hand-off waits for the owner's
confirmation, the owner's receipt reaches Smart's delivery items, the confirmation code reaches no model, and a worker's
result read through the specialist reaches Smart inside its outside-content wrapper.

Fixture: the real registry and coordinator, the real factory builder on a stand-in factory object, the real
`ExternalTaskAgent` and `ExternalTaskService` over the fakes of `tests/support/external_task_world.py`; Smart and the
specialist are scripted models. A web search is answered by the coordinator stand-in itself. Alex Doe is `usr-1`.
"""
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.composition.user_agent_factory import UserAgentFactory, _UserContext
from src.domain.agent import AgentResponse, AgentStatus
from src.domain.external_task import ExternalTaskStatus
from src.domain.external_task_entry import TaskEntry
from src.domain.external_task_receipt import EXTERNAL_TASK_RECEIPT_DELIVERY
from src.domain.llm import Message, MessagePart
from src.domain.untrusted_text import EXTERNAL_REMINDER
from src.domain.user import UserBotConfig
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS, Intent
from src.infrastructure.agent_registry import AgentRegistry
from src.ports.agent_factory_port import AgentFactoryPort
from tests.support.external_task_world import ORIGIN, TEXT, TITLE, USER_ID, ExternalTaskWorld
from tests.support.scripted_llm import ScriptedLLM, execution_context, text, tool_results, tools
from tests.support.tool_e2e import StubPromptBuilder, smart_message, smart_rig

S = ExternalTaskStatus
HAND_OFF = ("delegate_to_specialist", {"intent": Intent.WORK_WITH_EXTERNAL_TASKS,
                                       "query": f"Hand this to research, word for word: {TEXT}"})
CREATE = ("create_external_task", {"worker": "research", "title": TITLE, "text": TEXT})
WEB = ("delegate_to_specialist", {"intent": "search_web", "query": "irrigation kits"})


class _Coordinator(AgentCoordinator):
    """The real coordinator; a web search is answered here (its specialist is not under test)."""

    async def handle_delegation(self, intent, query, context, calling_agent_id):
        if intent == "search_web":
            return AgentResponse.success(task_id="t", agent_id="web", result="Three kits compared on a vendor page.")
        return await super().handle_delegation(intent, query, context, calling_agent_id)


class _Factory(AgentFactoryPort):
    def __init__(self, fake: SimpleNamespace) -> None:
        self.fake = fake

    async def create_agent_on_demand(self, agent_type: str, user_id: str) -> bool:
        return await UserAgentFactory.create_agent_on_demand(self.fake, agent_type, user_id)


def _chain(world: ExternalTaskWorld, specialist_llm: ScriptedLLM) -> AgentCoordinator:
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    coordinator = _Coordinator(registry=registry)
    context_builder = MagicMock()
    context_builder.build.return_value = execution_context(specialist_llm, "external_tasks")
    profile = SimpleNamespace(config=UserBotConfig(external_tasks_enabled=True))
    ctx = _UserContext(user_profile=profile, prompt_builder=StubPromptBuilder("TASK SPECIALIST PROMPT"))
    fake = SimpleNamespace(
        _cache={USER_ID: {"_user_context": ctx, "_lazy_agent_ids": [], "last_used": time.time()}},
        _creation_locks={}, _LAZY_BUILDERS=UserAgentFactory._LAZY_BUILDERS,
        _LAZY_AGENT_IDS=UserAgentFactory._LAZY_AGENT_IDS, coordinator=coordinator, prompt_content_store=None,
        quota_service=None, context_builder=context_builder, external_tasks=world.service)
    coordinator.set_agent_factory(_Factory(fake))
    return coordinator


async def _turn(world, smart_script, specialist_script, history=()):
    smart_llm, specialist_llm = ScriptedLLM(smart_script), ScriptedLLM(specialist_script)
    sessions = SimpleNamespace(load_session=_sessions(list(history)))
    smart = smart_rig(smart_llm, user_id=USER_ID, coordinator=_chain(world, specialist_llm), session_store=sessions,
                      user_config=UserBotConfig(external_tasks_enabled=True))
    response = await smart.execute(smart_message(
        "hand the irrigation comparison to research", user_id=USER_ID, account_id="acc-1",
        session_id=f"{USER_ID}:C-garden", origin_platform="slack", origin_channel_id="C-garden"))
    assert response.status is AgentStatus.SUCCESS
    for llm in (smart_llm, specialist_llm):
        assert llm.unanswered == 0 and llm.pending() == 0
    return response, smart_llm, specialist_llm


def _sessions(history):
    async def load(session_id):
        return SimpleNamespace(history=history)
    return load


async def test_a_chat_hand_off_after_a_web_search_waits_for_the_owner_and_the_receipt_reaches_smart():
    world = await ExternalTaskWorld().seed()
    response, smart_llm, specialist_llm = await _turn(
        world, [tools(WEB), tools(HAND_OFF), text("It waits for your confirmation.")],
        [tools(CREATE), text("Held for the owner.")])

    [task] = world.tasks.tasks.values()
    assert task.status is S.HELD and task.text == TEXT and task.origin == ORIGIN
    [item] = [i for i in response.delivery_items if i.type == EXTERNAL_TASK_RECEIPT_DELIVERY]
    code = item.data["command"].split()[-1]
    for llm in (smart_llm, specialist_llm):
        assert all(code not in str(result) for _, result in tool_results(llm))     # no model reads the code
    assert code not in str(response.metadata)
    assert set(response.metadata) <= {"model", "tokens", "response_length", "response_summary",
                                      "response_summary_task"}   # no tool's history key
    assert task.task_id not in str(response.metadata)


async def test_the_next_turns_hand_off_waits_for_the_owner_too():
    world = await ExternalTaskWorld().seed()
    await _turn(world, [tools(HAND_OFF), text("It waits for your confirmation.")], [tools(CREATE), text("Pending.")])
    second, _, _ = await _turn(world, [tools(HAND_OFF), text("This one waits too.")], [tools(CREATE), text("Pending.")],
                               history=[Message(role="user", parts=[MessagePart(text="hand it over")]),
                                        Message(role="model", parts=[MessagePart(text="It waits.")])])
    assert [t.status for t in world.tasks.tasks.values()] == [S.HELD, S.HELD]
    assert len([i for i in second.delivery_items if i.type == EXTERNAL_TASK_RECEIPT_DELIVERY]) == 1


async def test_reading_a_result_through_the_specialist_reaches_smart_wrapped():
    world = await ExternalTaskWorld().seed()
    task = await world.service.create(USER_ID, "research", "earlier", "an earlier brief", ORIGIN, entry=TaskEntry.CHAT)
    await world.service.confirm(USER_ID, task.code)
    worker = next(iter(world.workers.workers.values()))
    principal = await world.service.resolve_key((await world.service.mint_key(USER_ID, worker.worker_id)).key)
    claim = await world.service.claim(principal)
    await world.service.submit(principal, task.task_id, claim.lease_id, "Kit A is cheapest.")
    read = ("delegate_to_specialist", {"intent": Intent.WORK_WITH_EXTERNAL_TASKS,
                                       "query": f"What did task {task.task_id} return?"})

    response, smart_llm, _ = await _turn(world, [tools(read), text("Kit A is cheapest.")],
                                         [tools(("get_external_task", {"task_id": task.task_id})), text("Done.")])

    [(_, answer)] = tool_results(smart_llm)
    assert EXTERNAL_REMINDER in str(answer) and 'nonce="' in str(answer) and "Kit A is cheapest." in str(answer)
