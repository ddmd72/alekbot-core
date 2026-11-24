"""The tasks resource's path and URI (EXTERNAL_TASK_QUEUE_RFC §5.4): the facts host, its own path, never under `/mcp/`."""
from src.domain.mcp_resource import TASKS_PATH, MCPResource, drive_resource_uri, tasks_resource_uri

FACTS = "https://mcp.example.test/mcp"


def test_the_tasks_uri_is_the_facts_host_with_the_tasks_path():
    assert MCPResource.TASKS.value == "tasks"
    assert TASKS_PATH == "/tasks-mcp" and not TASKS_PATH.startswith("/mcp/")
    assert tasks_resource_uri(FACTS) == "https://mcp.example.test/tasks-mcp"
    assert tasks_resource_uri(" HTTPS://MCP.Example.Test/mcp/ ") == "https://mcp.example.test/tasks-mcp"
    assert tasks_resource_uri(FACTS) != drive_resource_uri(FACTS)
