"""search_tasks tells the model that results carry `ref`, which update/delete take as task_ref."""

from src.agents.tasks_agent import _TOOL_DECLARATIONS


def _declaration(name: str) -> dict:
    return next(t for t in _TOOL_DECLARATIONS if t["name"] == name)


def test_search_tasks_description_names_ref_and_task_ref():
    description = _declaration("search_tasks")["description"]
    assert "`ref`" in description
    assert "task_ref" in description
    assert "list_ids" not in description


def test_update_and_delete_take_task_ref():
    for name in ("update_task", "delete_task"):
        assert "task_ref" in _declaration(name)["parameters"]["properties"]
