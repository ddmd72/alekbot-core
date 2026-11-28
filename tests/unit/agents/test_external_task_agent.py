"""ExternalTaskAgent, the task specialist (EXTERNAL_TASK_QUEUE_RFC §5.3): the gate, every create waiting for the owner's
confirmation, the verbatim brief, the answer composed by code and the receipts that survive a cut run.

A real `ExternalTaskAgent` (`specialist_rig`) over a scripted model and its real DelegationEngine; the real
`ExternalTaskService` over the fakes of `tests/support/external_task_world.py`. Alex Doe is `usr-1` with the worker
`research`. Assertions are on the stored tasks, the answer Smart receives, the receipts and the model's requests.
"""
import asyncio
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.domain.agent import AgentIntent, AgentStatus
from src.domain.external_task import HOLD_S, ExternalTaskStatus
from src.domain.external_task_entry import REMINDER_RUN_ORIGIN, TaskEntry
from src.domain.external_task_receipt import EXTERNAL_TASK_RECEIPT_DELIVERY
from src.domain.external_task_texts import (
    ANSWER_CUT,
    ANSWER_EMPTY,
    ANSWER_NOTE_HEAD,
    ANSWER_RESULTS_HEAD,
    ANSWER_CUT_BEFORE_ANY_TOOL,
    ANSWER_UNAVAILABLE_OWNER,
    ANSWER_UNAVAILABLE_SYSTEM,
    ANSWER_UNAVAILABLE_TURN,
    ERROR_FAILED,
    ERROR_NOT_THE_BRIEF,
    CREATE_DESCRIPTION_CHAT,
    CREATE_DESCRIPTION_REMINDER,
    NOTE_PENDING,
    NOTE_QUEUED,
)
from src.domain.llm import PROMPT_CACHE_BOUNDARY, LLMRequest, LLMResponse
from src.domain.notification import NotificationChannel
from src.domain.untrusted_text import EXTERNAL_REMINDER
from src.domain.run_deadline import CURRENT_RUN_DEADLINE
from src.domain.turn_clock import CURRENT_TURN_CLOCK, TurnClock
from src.domain.user import InactiveReason
from src.infrastructure import agent_config
from src.infrastructure.notification_sla import NOTIFICATION_SLA
from src.services.localization_service import LocalizationService
from src.services.user_notification_service import UserNotificationService
from src.agents.external_task_agent import soft_deadline
from tests.support.external_task_agent_rig import specialist_message, specialist_rig
from tests.support.external_task_world import ACCOUNT_ID, ORIGIN, TEXT, TITLE, USER_ID, USERS, ExternalTaskWorld, NOTE
from tests.support.scripted_llm import ScriptedLLM, text, tool_results, tools
from tests.support.tool_e2e import StubPromptBuilder

S = ExternalTaskStatus
STAMP = "[Mon 23 Nov 2026, 09:00 UTC]"
INSTRUCTION = f"{STAMP} Hand this to research, word for word:\n\n{TEXT}"
CREATE = ("create_external_task", {"worker": "research", "title": TITLE, "text": TEXT, "note": NOTE})
RESULT = "Kit A is cheapest. Kit B is sturdiest."
HOURS = HOLD_S // 3600


def pending(task_id):
    return NOTE_PENDING.format(task_id=task_id, worker="research", hours=HOURS)


@pytest.fixture
async def world():
    return await ExternalTaskWorld().seed()


async def run(world, script, query=INSTRUCTION, **context):
    llm = ScriptedLLM(script)
    agent = specialist_rig(llm, world)
    response = await agent.execute(specialist_message(query, **context))
    assert llm.unanswered == 0 and llm.pending() == 0
    return llm, response


def receipts(response):
    return [i for i in response.delivery_items if i.type == EXTERNAL_TASK_RECEIPT_DELIVERY]


async def finished_task(world, result=RESULT):
    """A task of Alex's, released by the owner, that the worker `research` took and finished with `result`."""
    task = await world.service.create(USER_ID, "research", "earlier", "an earlier brief", ORIGIN, entry=TaskEntry.CHAT, note=NOTE)
    await world.service.confirm(USER_ID, task.code)
    worker = next(w for w in world.workers.workers.values() if w.label == "research")
    principal = await world.service.resolve_key((await world.service.mint_key(USER_ID, worker.worker_id)).key)
    claim = await world.service.claim(principal)
    await world.service.submit(principal, task.task_id, claim.lease_id, result)
    return task.task_id


# --- the playbook set --------------------------------------------------------------------------------------------

async def test_it_handles_queries_only(world):
    agent = specialist_rig(ScriptedLLM([]), world)
    message = specialist_message(INSTRUCTION)
    assert await agent.can_handle(message) is True
    message.intent = AgentIntent.INFORM
    assert await agent.can_handle(message) is False


async def test_a_create_waits_for_the_owner_the_owner_gets_the_receipt_and_smart_gets_every_result(world):
    builder = StubPromptBuilder("TASK SPECIALIST PROMPT")
    llm = ScriptedLLM([tools(CREATE), text("Handed the comparison to research.")])
    response = await specialist_rig(llm, world, prompt_builder=builder).execute(specialist_message(INSTRUCTION))

    [task] = world.tasks.tasks.values()
    assert (task.status, task.label, task.title, task.text, task.origin) == (S.HELD, "research", TITLE, TEXT, ORIGIN)
    assert response.status is AgentStatus.SUCCESS and response.history_context is None
    assert response.result == "\n\n".join([
        ANSWER_RESULTS_HEAD, pending(task.task_id),
    ]) + f"\n\n{ANSWER_NOTE_HEAD}\nHanded the comparison to research."
    [item] = receipts(response)
    assert item.data["status"] == "held" and item.data["text"] == TEXT and item.data["command"].startswith("$task send ")
    [call] = builder.calls
    assert call["agent_type"] == "external_tasks" and call["include_biographical"] is False
    assert call["include_directives"] is False and call["include_location"] is False


async def test_an_empty_instruction_fails_before_any_model_call(world):
    llm, response = await run(world, [], query="   ")
    assert response.status is AgentStatus.FAILED and "instruction is empty" in response.error
    assert llm.requests == []


async def test_a_model_exception_answers_from_what_ran_and_keeps_the_receipt(world):
    llm, response = await run(world, [tools(CREATE), RuntimeError("provider down")])
    assert response.status is AgentStatus.SUCCESS
    assert response.result.endswith(ANSWER_CUT) and ANSWER_RESULTS_HEAD in response.result
    assert len(receipts(response)) == 1 and next(iter(world.tasks.tasks.values())).status is S.HELD


async def test_a_prompt_builder_failure_fails_without_calling_the_model(world):
    class Broken(StubPromptBuilder):
        async def build_for_agent(self, **kwargs):
            raise RuntimeError("token store down")

    llm = ScriptedLLM([])
    response = await specialist_rig(llm, world, prompt_builder=Broken()).execute(specialist_message(INSTRUCTION))
    assert response.status is AgentStatus.FAILED and "PromptBuilder failed" in response.error
    assert llm.requests == [] and world.tasks.tasks == {}


# --- the gate: a code line, no model call ------------------------------------------------------------------------

async def _lock(world):
    await world.db.collection(USERS).document(USER_ID).update(
        {"is_active": False, "inactive_reason": InactiveReason.DURESS.value})


@pytest.mark.parametrize("context,service,prepare,line", [
    ({"interactive_delivery": None}, True, None, ANSWER_UNAVAILABLE_TURN),                  # a background run
    ({"_call_chain": ["alek_agent", "smart_response_agent", "external_task_agent"]}, True, None,
     ANSWER_UNAVAILABLE_TURN),                                                             # a nested Smart
    ({"_call_chain": None}, True, None, ANSWER_UNAVAILABLE_TURN),                           # not through the coordinator
    ({}, False, None, ANSWER_UNAVAILABLE_SYSTEM),                                          # no task queue here
    ({}, True, lambda world: world.flag(USER_ID, False), ANSWER_UNAVAILABLE_OWNER),         # the flag, read fresh
    ({}, True, _lock, ANSWER_UNAVAILABLE_OWNER),                                           # the account lock
    ({"user_id": None}, True, None, ANSWER_UNAVAILABLE_TURN),                               # no user
])
async def test_the_gate_refuses_with_a_code_line_and_no_model_call(world, context, service, prepare, line):
    if prepare is not None:
        await prepare(world)
    llm = ScriptedLLM([])
    agent = specialist_rig(llm, world, service=world.service if service else None)
    response = await agent.execute(specialist_message(INSTRUCTION, **context))
    assert response.status is AgentStatus.SUCCESS and response.result == line
    assert llm.requests == [] and world.tasks.tasks == {} and response.delivery_items == []


async def test_a_failing_worker_list_is_a_queue_failure_line_and_no_model_call(world, monkeypatch):
    async def broken(user_id):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(world.workers, "list_workers", broken)
    llm, response = await run(world, [])
    assert response.status is AgentStatus.SUCCESS and response.result == ERROR_FAILED and llm.requests == []


# --- the confirmation code reaches the receipt only ----------------------------------------------------------------

async def test_the_code_reaches_the_receipt_only_and_nothing_of_the_instruction_is_logged(world, caplog):
    caplog.set_level(logging.DEBUG)
    llm, response = await run(world, [tools(CREATE), text("It waits for the owner.")])
    [task] = world.tasks.tasks.values()
    assert task.status is S.HELD and pending(task.task_id) in response.result
    [item] = receipts(response)
    code = item.data["command"].split()[-1]
    for seen in (response.result, str(response.metadata), str(tool_results(llm)), caplog.text):
        assert code not in seen
    assert TEXT not in caplog.text and STAMP not in caplog.text       # neither the instruction nor the brief is logged


async def test_a_create_after_reading_a_worker_result_waits_for_the_owner_too(world):
    task_id = await finished_task(world)
    _, response = await run(world, [tools(("get_external_task", {"task_id": task_id})), tools(CREATE), text("ok")])
    [created] = [t for t in world.tasks.tasks.values() if t.title == TITLE]
    assert created.status is S.HELD and len(receipts(response)) == 1


# --- a worker's result reaches Smart wrapped; the note is the specialist's own text ---------------------------------

async def test_reading_a_finished_task_returns_the_wrapped_result_and_the_plain_note(world):
    task_id = await finished_task(world)
    llm, response = await run(world, [tools(("get_external_task", {"task_id": task_id})),
                                      text("Research finished the comparison.")], query=f"{STAMP} What did {task_id} return?")
    [(_, result)] = tool_results(llm)
    assert result in response.result and EXTERNAL_REMINDER in result and 'nonce="' in result and RESULT in result
    assert response.result.endswith(f"{ANSWER_NOTE_HEAD}\nResearch finished the comparison.")


# --- the brief, word for word -------------------------------------------------------------------------------------

async def test_an_altered_brief_is_refused_and_the_verbatim_retry_is_created(world):
    reworded = ("create_external_task", {"worker": "research", "title": TITLE,
                                         "text": "Compare 3 irrigation kits for the Shelbyville community garden.",
                                         "note": NOTE})
    spaced = ("create_external_task", {"worker": "research", "title": TITLE, "text": "  " + TEXT.replace(" ", "\n "),
                                       "note": NOTE})
    llm, response = await run(world, [tools(reworded), tools(spaced), text("Handed over.")])
    assert [r for _, r in tool_results(llm)][0] == ERROR_NOT_THE_BRIEF
    [task] = world.tasks.tasks.values()
    assert task.status is S.HELD and len(receipts(response)) == 1


# --- workers and the state block ----------------------------------------------------------------------------------

async def test_with_no_worker_create_is_not_declared_and_the_state_says_so():
    world = await ExternalTaskWorld().seed(())
    llm, response = await run(world, [tools(("list_external_workers", {})), text("No worker is registered.")])
    request = llm.requests[0]
    assert {t["name"] for t in request.tools} == {"list_external_tasks", "get_external_task", "cancel_external_task",
                                                 "list_external_workers"}
    assert request.system_instruction.endswith(
        "queue_state {\n  workers: none registered (nothing can be handed over until the owner registers a worker in "
        "the Cabinet)\n}")
    assert "no worker is registered" in response.result


async def test_the_state_block_lists_the_labels_after_the_cache_boundary():
    world = await ExternalTaskWorld().seed(("research", "calendar-watch"))
    llm, _ = await run(world, [text("Which worker is meant: research or calendar-watch?")])
    instruction = llm.requests[0].system_instruction
    assert instruction.startswith("TASK SPECIALIST PROMPT")
    assert instruction.index(PROMPT_CACHE_BOUNDARY) < instruction.index("queue_state {")
    state = instruction.split(PROMPT_CACHE_BOUNDARY, 1)[1].strip()
    head, workers, tail = state.splitlines()
    assert (head, tail) == ("queue_state {", "}")
    assert sorted(workers.removeprefix("  workers: ").split(", ")) == ["calendar-watch", "research"]
    create = next(t for t in llm.requests[0].tools if t["name"] == "create_external_task")
    assert sorted(create["parameters"]["properties"]["worker"]["enum"]) == ["calendar-watch", "research"]


async def test_with_a_registered_worker_all_five_tools_are_declared(world):
    """Reviewer: the declared set with a worker (the retired gate matrix's first row, now on the specialist)."""
    llm, _ = await run(world, [text("ok")])
    assert {t["name"] for t in llm.requests[0].tools} == {"create_external_task", "list_external_tasks",
                                                         "get_external_task", "cancel_external_task",
                                                         "list_external_workers"}


@pytest.mark.parametrize("thinking,expected", [(None, "low"), ("high", "high")])
async def test_thinking_is_the_users_override_or_low(world, thinking, expected):
    llm = ScriptedLLM([text("ok")])
    await specialist_rig(llm, world, thinking=thinking).execute(specialist_message(INSTRUCTION))
    assert llm.requests[0].thinking == expected


# --- cut runs: success, from what ran ------------------------------------------------------------------------------

class StallingLLM(ScriptedLLM):
    """Answers the script, then never answers again."""

    async def generate_content(self, request: LLMRequest) -> LLMResponse:
        if not self.script:
            self.requests.append(request.model_copy(deep=True))
            await asyncio.Event().wait()
        return await super().generate_content(request)


async def test_a_run_cut_by_the_soft_deadline_after_a_create_still_answers_with_the_receipt(world, monkeypatch):
    monkeypatch.setattr(agent_config.EXTERNAL_TASKS, "soft_deadline_s", 0.1)
    llm = StallingLLM([tools(CREATE)])
    response = await specialist_rig(llm, world).execute(specialist_message(INSTRUCTION))
    assert response.status is AgentStatus.SUCCESS and response.result.endswith(ANSWER_CUT)
    [task] = world.tasks.tasks.values()
    assert pending(task.task_id) in response.result
    assert len(receipts(response)) == 1


async def test_the_turn_cap_cuts_the_run(world):
    listing = tools(("list_external_tasks", {}))
    _, response = await run(world, [listing] * agent_config.EXTERNAL_TASKS.max_turns)
    assert response.status is AgentStatus.SUCCESS and response.result.endswith(ANSWER_CUT)


async def test_a_run_with_no_tool_and_no_note_says_so(world):
    _, response = await run(world, [text("")])
    assert response.result == ANSWER_EMPTY


def test_the_soft_deadline_takes_the_earliest_bound():
    own = agent_config.EXTERNAL_TASKS.soft_deadline_s
    margin = agent_config.EXTERNAL_TASKS.caller_margin_s
    assert soft_deadline(100.0, 100.0) == 100.0 + own
    token = CURRENT_RUN_DEADLINE.set(140.0)
    try:
        assert soft_deadline(100.0, 100.0) == 140.0 - margin                      # the caller's own timeout
        clock_token = CURRENT_TURN_CLOCK.set(TurnClock.start(budget_s=100, wrap_up_reserve_s=80, now=100.0))
        try:
            assert soft_deadline(100.0, 100.0) == 100.0 + 20 - margin              # the caller's turn clock
        finally:
            CURRENT_TURN_CLOCK.reset(clock_token)
    finally:
        CURRENT_RUN_DEADLINE.reset(token)


async def test_a_cancel_while_the_prompt_is_built_propagates(world):
    class Cancelled(StubPromptBuilder):
        async def build_for_agent(self, **kwargs):
            raise asyncio.CancelledError()

    llm = ScriptedLLM([])
    with pytest.raises(asyncio.CancelledError):
        await specialist_rig(llm, world, prompt_builder=Cancelled()).execute(specialist_message(INSTRUCTION))
    assert llm.requests == [] and world.tasks.tasks == {}


async def test_a_run_cut_before_any_tool_says_nothing_was_done(world):
    _, response = await run(world, [RuntimeError("provider down")])
    assert response.status is AgentStatus.SUCCESS and response.result == ANSWER_CUT_BEFORE_ANY_TOOL
    assert response.delivery_items == []


# --- a reminder's run: queued at once, a show-only receipt ---------------------------------------------------------

REMINDER_RUN = {"interactive_delivery": None, "run_origin": REMINDER_RUN_ORIGIN}


def create_declaration(llm):
    return next(t for t in llm.requests[0].tools if t["name"] == "create_external_task")


class RecordingReceipts:
    """Stands for `UserNotificationService.post_task_receipt`: records every receipt the specialist posts, verbatim."""

    def __init__(self) -> None:
        self.posted = []

    async def post_task_receipt(self, user_id, account_id, data, **channel):
        self.posted.append((user_id, account_id, dict(data), channel))
        return True


async def test_a_reminders_create_is_queued_at_once_with_a_note_and_a_show_only_receipt(world):
    llm, notifications = ScriptedLLM([tools(CREATE), text("Handed over.")]), RecordingReceipts()
    response = await specialist_rig(llm, world, notifications=notifications).execute(
        specialist_message(INSTRUCTION, **REMINDER_RUN))
    assert llm.unanswered == 0 and llm.pending() == 0

    [task] = world.tasks.tasks.values()
    assert task.status is S.QUEUED and task.hold is None and task.text == TEXT and task.origin == ORIGIN
    assert response.delivery_items == []                   # the receipt was posted by the create, not handed back
    [(user_id, account_id, data, channel)] = notifications.posted
    assert (user_id, account_id) == (USER_ID, ACCOUNT_ID)
    assert channel == {"channel_id_override": ORIGIN.channel_id, "platform_override": ORIGIN.platform}
    assert data == {"status": "queued", "worker": "research", "title": TITLE, "text": TEXT, "note": NOTE,
                    "task_id": task.task_id, "command": None}    # nothing to confirm: shown, never a command
    queued = NOTE_QUEUED.format(task_id=task.task_id, worker="research", title=TITLE)
    assert response.result == "\n\n".join([ANSWER_RESULTS_HEAD, queued]) + f"\n\n{ANSWER_NOTE_HEAD}\nHanded over."
    assert [j["kind"] for j in world.jobs("external_task_timer")] == ["deadline"]     # no hold timer: nothing waits
    assert all(NOTE_PENDING.format(task_id=task.task_id, worker="research", hours=HOURS) not in str(r)
               for _, r in tool_results(llm))


async def test_a_chat_create_still_waits_and_each_entry_reads_its_own_create_description(world):
    chat_llm, chat = await run(world, [tools(CREATE), text("Held.")])
    reminder_llm, _ = await run(world, [text("Nothing.")], **REMINDER_RUN)

    assert create_declaration(chat_llm)["description"] == CREATE_DESCRIPTION_CHAT
    assert create_declaration(reminder_llm)["description"] == CREATE_DESCRIPTION_REMINDER
    assert CREATE_DESCRIPTION_CHAT != CREATE_DESCRIPTION_REMINDER
    assert len(receipts(chat)) == 1 and next(iter(world.tasks.tasks.values())).status is S.HELD


async def test_a_model_cannot_choose_the_entry_through_its_arguments(world):
    forged = ("create_external_task", {"worker": "research", "title": TITLE, "text": TEXT, "note": NOTE,
                                       "entry": "reminder"})
    _, response = await run(world, [tools(forged), text("Held.")])
    assert next(iter(world.tasks.tasks.values())).status is S.HELD and len(receipts(response)) == 1


@pytest.mark.parametrize("run_origin", ["background:daily_email_review", "background:external_task_deliver",
                                        "voice:delegate"])
async def test_the_gate_refuses_every_other_background_run(world, run_origin):
    llm = ScriptedLLM([])
    response = await specialist_rig(llm, world).execute(
        specialist_message(INSTRUCTION, interactive_delivery=None, run_origin=run_origin))
    assert response.result == ANSWER_UNAVAILABLE_TURN and llm.requests == [] and world.tasks.tasks == {}


# --- a reminder's create posts its own receipt, once, when the task is queued ---------------------------------------

class ReceiptChannel:
    """Records the files and literal messages posted to one channel, in order."""

    def __init__(self, broken_file: bool = False) -> None:
        self.posts = []
        self.broken_file = broken_file

    async def send_file(self, content: bytes, filename: str, title: str, thread_id=None) -> None:
        if self.broken_file:
            raise RuntimeError("upload failed")
        self.posts.append(("file", filename))

    async def send_literal(self, text: str, thread_id=None) -> None:
        self.posts.append(("literal", text))


def notifications_on(channel):
    """The real `UserNotificationService` with the real localization over a recording channel; the primary channel is
    another one than the run's, so a receipt that lands on the run's channel was sent there on purpose."""
    state = AsyncMock()
    state.get_primary.return_value = NotificationChannel(user_id=USER_ID, platform="slack", channel_id="C-other",
                                                         updated_at=datetime(2026, 11, 23, tzinfo=timezone.utc))
    factory = MagicMock()
    factory.create.return_value = channel
    service = UserNotificationService(state_repo=state, channel_factory=factory, coordinator=None,
                                      notification_sla=NOTIFICATION_SLA,
                                      localization=LocalizationService(FileLocalizationAdapter()))
    return service, factory


async def run_notified(world, script, channel, **context):
    llm = ScriptedLLM(script)
    notifications, factory = notifications_on(channel)
    response = await specialist_rig(llm, world, notifications=notifications).execute(
        specialist_message(INSTRUCTION, **context))
    assert llm.unanswered == 0 and llm.pending() == 0
    return response, factory


@pytest.mark.parametrize("script", [
    [tools(CREATE), text("Handed over.")],                     # the run ends well
    [tools(CREATE), RuntimeError("provider down")],            # the run fails after the create
], ids=["answered", "failed_after_the_create"])
async def test_a_reminders_create_posts_one_receipt_to_the_runs_channel_and_returns_none(world, script):
    channel = ReceiptChannel()
    response, factory = await run_notified(world, script, channel, **REMINDER_RUN)

    [task] = world.tasks.tasks.values()
    assert task.status is S.QUEUED
    assert response.status is AgentStatus.SUCCESS and response.delivery_items == []   # nothing for the caller to post
    [(filed, filename), (lit, message)] = channel.posts
    assert (filed, filename, lit) == ("file", f"task-{task.task_id}.txt", "literal")
    assert task.task_id in message and "$task send" not in message
    factory.create.assert_called_once_with(platform="slack", channel_id="C-garden")   # the run's channel


async def test_a_chat_create_posts_nothing_itself_and_returns_its_receipt_with_the_command(world):
    channel = ReceiptChannel()
    response, factory = await run_notified(world, [tools(CREATE), text("Held.")], channel)

    assert channel.posts == [] and factory.create.call_count == 0
    [item] = receipts(response)
    assert item.data["status"] == "held" and item.data["command"].startswith("$task send ")
    assert next(iter(world.tasks.tasks.values())).status is S.HELD


async def test_a_reminders_receipt_that_cannot_be_posted_leaves_the_task_queued_and_the_answer_standing(world):
    channel = ReceiptChannel(broken_file=True)
    response, _ = await run_notified(world, [tools(CREATE), text("Handed over.")], channel, **REMINDER_RUN)

    [task] = world.tasks.tasks.values()
    assert task.status is S.QUEUED and channel.posts == []      # no message without its file
    queued = NOTE_QUEUED.format(task_id=task.task_id, worker="research", title=TITLE)
    assert response.status is AgentStatus.SUCCESS and queued in response.result and response.delivery_items == []
