"""P14: the task specialist's two writing external task tools, end to end (TEST_COVERAGE_SYSTEMIC_RFC §5 P14;
EXTERNAL_TASK_QUEUE_RFC §5.3).

A real `ExternalTaskAgent` (`specialist_rig`) over a scripted `LLMPort`; its tool loop, the two local handlers and the
real `ExternalTaskService` run for real, over the in-memory stores of `tests/support/external_task_world.py`. Assertions
are on the stored task and the tool result the model received. Every declared field of `create_external_task` and
`cancel_external_task` is sent by a scripted call (module constants; the last test checks them against the
declarations). `list_external_tasks`, `get_external_task` and `list_external_workers` read only (`READ_ONLY` of
`tool_fields.py`).
"""
from src.domain.external_task_entry import TaskEntry
from src.domain.agent import AgentStatus
from src.domain.external_task import HOLD_S, ExternalTaskStatus
from src.domain.external_task_texts import NOTE_PENDING
from tests.support.external_task_agent_rig import specialist_message, specialist_rig
from tests.support.external_task_world import ORIGIN, TEXT, TITLE, ExternalTaskWorld, NOTE
from tests.support.scripted_llm import ScriptedLLM, text, tool_results, tools
from tests.support.tool_fields import unused

S = ExternalTaskStatus

CREATE = {"worker": "research", "title": TITLE, "text": TEXT, "note": NOTE}
UNKNOWN_WORKER = {"worker": "nobody", "title": TITLE, "text": TEXT, "note": NOTE}
TOO_LONG_TITLE = {"worker": "research", "title": "t" * 500, "text": TEXT, "note": NOTE}
INSTRUCTION = f"Hand this to research: {TEXT}"


def cancel(task_id: str) -> dict:
    return {"task_id": task_id}


async def run(world: ExternalTaskWorld, script):
    llm = ScriptedLLM(script)
    response = await specialist_rig(llm, world).execute(specialist_message(INSTRUCTION))
    assert response.status == AgentStatus.SUCCESS
    assert llm.unanswered == 0 and llm.pending() == 0
    return llm, response


async def test_a_created_task_stores_worker_title_and_text():
    world = await ExternalTaskWorld().seed()

    llm, _ = await run(world, [tools(("create_external_task", CREATE)), text("It waits for your confirmation.")])

    [task] = world.tasks.tasks.values()
    assert (task.label, task.title, task.text, task.status) == ("research", TITLE, TEXT, S.HELD)
    (name, result), = tool_results(llm)
    assert name == "create_external_task"
    assert result == NOTE_PENDING.format(task_id=task.task_id, worker="research", hours=HOLD_S // 3600)


async def test_a_cancelled_task_stores_the_cancellation_of_the_task_id_it_names():
    world = await ExternalTaskWorld().seed()
    first = await world.service.create("usr-1", "research", "keep", "stays queued", ORIGIN, entry=TaskEntry.REMINDER, note=NOTE)
    second = await world.service.create("usr-1", "research", "drop", "to be cancelled", ORIGIN, entry=TaskEntry.REMINDER, note=NOTE)

    llm, _ = await run(world, [tools(("cancel_external_task", cancel(second.task_id))), text("Cancelled.")])

    assert world.tasks.tasks[second.task_id].status is S.CANCELLED
    assert world.tasks.tasks[first.task_id].status is S.QUEUED
    (name, result), = tool_results(llm)
    assert name == "cancel_external_task" and "cancelled" in result


async def test_a_refused_create_stores_nothing_and_the_model_reads_a_tool_error():
    world = await ExternalTaskWorld().seed()

    llm, _ = await run(world, [tools(("create_external_task", UNKNOWN_WORKER)),
                               tools(("create_external_task", TOO_LONG_TITLE)), text("Could not hand it over.")])

    assert world.tasks.tasks == {}
    assert [r.startswith("Tool error:") for _, r in tool_results(llm)] == [True, True]


async def test_cancelling_an_unknown_task_is_a_tool_error_and_changes_nothing():
    world = await ExternalTaskWorld().seed()
    kept = await world.service.create("usr-1", "research", "keep", "stays queued", ORIGIN, entry=TaskEntry.REMINDER, note=NOTE)

    llm, _ = await run(world, [tools(("cancel_external_task", cancel("no-such-task"))), text("No such task.")])

    assert world.tasks.tasks[kept.task_id].status is S.QUEUED
    (_, result), = tool_results(llm)
    assert result.startswith("Tool error:")


def test_the_scripted_calls_use_every_declared_field():
    assert unused("external_tasks", "create_external_task", [CREATE]) == []
    assert unused("external_tasks", "cancel_external_task", [cancel("t-1")]) == []
