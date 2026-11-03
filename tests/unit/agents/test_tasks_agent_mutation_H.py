"""Mutation-survivor kills for TasksAgent: the read tools scope the provider call to the user."""
from tests.unit.agents.test_tasks_agent import (
    _LIST_ID, _TASK_ID, _USER_ID, _make_agent, _make_entry, _make_task,
)


async def test_list_tasks_asks_the_provider_for_this_users_tasks():
    agent, provider, _ = _make_agent()
    provider.list_tasks.return_value = [_make_task()]

    await agent._execute_tool("list_tasks", {}, _USER_ID)

    assert provider.list_tasks.await_args.kwargs["user_id"] == _USER_ID


async def test_search_tasks_fetches_the_found_tasks_for_this_user():
    agent, provider, indexing = _make_agent()
    indexing.search.return_value = [_make_entry()]
    provider.batch_get_tasks.return_value = [_make_task()]

    await agent._execute_tool("search_tasks", {"query": "milk"}, _USER_ID)

    kwargs = provider.batch_get_tasks.await_args.kwargs
    assert kwargs["user_id"] == _USER_ID
    assert kwargs["task_refs"] == [(_LIST_ID, _TASK_ID)]
