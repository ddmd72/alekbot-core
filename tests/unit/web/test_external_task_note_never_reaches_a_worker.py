"""A task's note is the owner's side only (owner's decision 2026-11-24): no worker surface carries it, neither the REST
endpoint `/external-tasks/v1/*` nor the tasks MCP resource `/tasks-mcp`, in a listing, a claim, a lease renewal or after the result.

The worlds of `tests/unit/web/test_external_task_api.py` and `tests/support/task_mcp_world.py`: the real service, the
real blueprint and the real MCP app over in-memory stores. Each test creates its task with a note through the service
and reads every worker answer as raw text. Alex Doe is `usr-1` with the worker `research`.
"""
from src.domain.external_task_entry import TaskEntry
from tests.support.external_task_world import NOTE
from tests.support.task_mcp_world import TEXT as MCP_TEXT, World as McpWorld, ok
from tests.unit.web.test_external_task_api import ORIGIN, TEXT, TITLE, World as RestWorld


async def test_the_rest_worker_answers_never_carry_the_note():
    world = await RestWorld().seed()
    created = await world.service.create("usr-1", "research", TITLE, TEXT, ORIGIN, entry=TaskEntry.REMINDER, note=NOTE)

    answers = [await world.call("GET", "/tasks", world.key)]
    claim = await world.claim()
    answers.append(await world.call("GET", "/tasks", world.key))
    answers.append(await world.call("POST", f"/tasks/{created.task_id}/renew", world.key,
                                    json={"lease_id": claim["lease_id"]}))
    answers.append(await world.call("POST", f"/tasks/{created.task_id}/result", world.key,
                                    json={"lease_id": claim["lease_id"], "result": "Kit A is cheapest."}))

    assert claim["task_id"] == created.task_id and claim["text"] == TEXT and "note" not in claim
    bodies = [str(claim)] + [(await answer.get_data()).decode() for answer in answers]
    assert all(answer.status_code == 200 for answer in answers)
    assert created.task_id in bodies[1] and created.task_id in bodies[2]      # both listings show the task
    for body in bodies:
        assert NOTE not in body and "Shelbyville community garden" not in body and '"note"' not in body


async def test_the_mcp_worker_answers_never_carry_the_note():
    world = await McpWorld().seed()
    world.initial = await world.texts.texts()
    created = await world.service.create("usr-1", "research", TITLE, MCP_TEXT, ORIGIN, entry=TaskEntry.REMINDER,
                                         note=NOTE)

    listed = ok(await world.call("list_tasks"))
    claim = await world.claim()
    listed_leased = ok(await world.call("list_tasks"))
    renewed = ok(await world.call("renew_lease", task_id=created.task_id, lease_id=claim["lease_id"]))
    submitted = ok(await world.call("submit_task_result", task_id=created.task_id, lease_id=claim["lease_id"],
                                    result="Kit A is cheapest."))

    assert claim["task_id"] == created.task_id and MCP_TEXT in claim["text"]
    assert created.task_id in listed and created.task_id in listed_leased     # both listings show the task
    for text in (listed, claim["text"], listed_leased, renewed, submitted):
        assert NOTE not in text and "Shelbyville community garden" not in text
