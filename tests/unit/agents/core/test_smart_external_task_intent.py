"""Smart and the external task queue (EXTERNAL_TASK_QUEUE_RFC §5.3): Smart has no queue tool of its own; it offers the task
specialist's internal intent under its gate (the owner's own chat turn, not reached by delegation, a user, the flag on).

A real `SmartResponseAgent` (`smart_rig`) over a scripted model and the real registry of every descriptor; the
specialists answer through a stub coordinator that records each delegation's context. Alex Doe is `usr-1`.
"""
import pytest

from src.domain.agent import AgentResponse, AgentStatus
from src.domain.external_task_entry import REMINDER_RUN_ORIGIN
from src.domain.user import UserBotConfig
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS, QUICK_RESPONSE, Intent
from src.infrastructure.agent_registry import AgentRegistry
from tests.support.scripted_llm import ScriptedLLM, text, tools
from tests.support.tool_e2e import smart_message, smart_rig

QUEUE = Intent.WORK_WITH_EXTERNAL_TASKS
QUEUE_TOOLS = {"create_external_task", "list_external_tasks", "get_external_task", "cancel_external_task",
               "list_external_workers"}


def registry() -> AgentRegistry:
    reg = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        reg.register(descriptor)
    return reg


class Specialists(AgentCoordinator):
    """The real registry; every delegation answers plain text."""

    def __init__(self) -> None:
        super().__init__(registry=registry())

    async def handle_delegation(self, intent, query, context, calling_agent_id):
        return AgentResponse.success(task_id="t", agent_id=intent, result=f"{intent}: plain text answer")


async def run(script, *, flag=True, user_id="usr-1", **context):
    llm = ScriptedLLM(script)
    coordinator = Specialists()
    agent = smart_rig(llm, user_id=user_id, coordinator=coordinator,
                      user_config=UserBotConfig(external_tasks_enabled=flag))
    message = smart_message("hand the irrigation comparison to research", user_id=user_id, account_id="acc-1",
                            session_id=f"{user_id}:C-garden", origin_platform="slack", origin_channel_id="C-garden",
                            **context)
    response = await agent.execute(message)
    assert response.status == AgentStatus.SUCCESS
    assert llm.unanswered == 0 and llm.pending() == 0
    return llm, response, coordinator


def offered(llm) -> set:
    """The registered intents the delegate tool's description lists (one `- <intent>: ` line each)."""
    delegate = next(t for t in llm.requests[0].tools if t["name"] == "delegate_to_specialist")
    return {i for d in ALL_DESCRIPTORS for i in d.capabilities if f"\n- {i}: " in delegate["description"]}


# --- the offer ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("flag,user_id,context,expected", [
    (True, "usr-1", {}, True),
    (False, "usr-1", {}, False),                                                        # the profile's flag is off
    (True, "usr-1", {"interactive": False}, False),                                     # a background run (E2)
    (True, "usr-1", {"_call_chain": ["smart_response_agent_usr-1"]}, False),            # reached through delegation
    (True, "usr-1", {"_call_chain": ["lelik_agent", "alek_agent"], "interactive_delivery": True}, False),  # send_to_chat
    (True, "", {}, False),                                                              # no user
])
async def test_the_intent_is_offered_only_under_smarts_gate(flag, user_id, context, expected):
    llm, _, _ = await run([text("ok")], flag=flag, user_id=user_id, **context)
    intents = offered(llm)
    assert (QUEUE in intents) is expected
    assert Intent.SEARCH_MEMORY in intents and Intent.WORK_WITH_DRIVE_FILES in intents      # the rest is unchanged
    assert not QUEUE_TOOLS & {t["name"] for t in llm.requests[0].tools}                      # never a local queue tool


@pytest.mark.parametrize("flag,context,expected", [
    (True, {"interactive": False, "run_origin": REMINDER_RUN_ORIGIN}, True),                 # a reminder's run
    (False, {"interactive": False, "run_origin": REMINDER_RUN_ORIGIN}, False),               # the flag is still read
    (True, {"interactive": False, "run_origin": REMINDER_RUN_ORIGIN,
            "_call_chain": ["smart_response_agent_usr-1"]}, False),                          # reached by delegation
    (True, {"interactive": False, "run_origin": "background:daily_email_review"}, False),
    (True, {"interactive": False, "run_origin": "background:external_task_deliver"}, False),  # the delivery alert
    (True, {"interactive": False, "run_origin": "voice:delegate"}, False),
])
async def test_the_intent_is_offered_in_a_reminders_run_and_in_no_other_background_run(flag, context, expected):
    llm, _, _ = await run([text("ok")], flag=flag, **context)
    assert (QUEUE in offered(llm)) is expected


async def test_a_reminders_run_is_offered_the_queue_but_not_the_interactive_only_tools():
    llm, _, _ = await run([text("ok")], interactive=False, run_origin=REMINDER_RUN_ORIGIN)
    names = {t["name"] for t in llm.requests[0].tools}
    assert QUEUE in offered(llm) and not {"draft_skill", "propose_memory_facts"} & names


def test_quick_never_sees_the_intent():
    assert QUEUE not in {i["name"] for i in registry().get_available_intents_for(QUICK_RESPONSE)}
