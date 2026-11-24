"""The task queue's worker resource in the scope-to-resource binding (EXTERNAL_TASK_QUEUE_RFC §5.4 "OAuth"): its scope
alone selects it, a mix is refused, and a resource that is not served is never bound."""

import pytest

from src.domain.mcp_resource import (
    MCPResource,
    ResourceRefused,
    ScopeRefused,
    bind_resource,
    is_drive_grant,
    is_tasks_grant,
)

FACTS_URI = "https://mcp.example.test/mcp"
DRIVE_URI = "https://mcp.example.test/drive-mcp"
TASKS_URI = "https://mcp.example.test/tasks-mcp"
URIS = {MCPResource.FACTS: FACTS_URI, MCPResource.DRIVE: DRIVE_URI, MCPResource.TASKS: TASKS_URI}
SCOPE_OF = {MCPResource.FACTS: "user_context", MCPResource.DRIVE: "drive_read", MCPResource.TASKS: "external_tasks"}


@pytest.mark.parametrize("resource, scopes", [(None, ["external_tasks"]), (None, ["external_tasks"] * 2),
                                              (TASKS_URI + "/", []), (TASKS_URI, ["external_tasks"])])
def test_the_tasks_scope_alone_or_the_tasks_resource_binds_the_tasks_resource(resource, scopes):
    assert bind_resource(resource, scopes, URIS, SCOPE_OF) == (MCPResource.TASKS, TASKS_URI, ["external_tasks"])


@pytest.mark.parametrize("resource, scopes", [
    (None, ["external_tasks", "user_context"]),
    (None, ["external_tasks", "drive_read"]),
    (TASKS_URI, ["user_context"]),
    (FACTS_URI, ["external_tasks"]),
    (DRIVE_URI, ["external_tasks"]),
])
def test_a_mix_or_a_mismatched_resource_and_scope_is_refused(resource, scopes):
    with pytest.raises(ScopeRefused):
        bind_resource(resource, scopes, URIS, SCOPE_OF)


@pytest.mark.parametrize("scopes, kind", [([], MCPResource.FACTS), (["user_context"], MCPResource.FACTS),
                                          (["drive_read"], MCPResource.DRIVE)])
def test_the_other_resources_bind_as_before(scopes, kind):
    assert bind_resource(None, scopes, URIS, SCOPE_OF)[0] is kind


def test_a_tasks_resource_that_is_not_served_is_never_bound():
    served = {k: v for k, v in URIS.items() if k is not MCPResource.TASKS}
    with pytest.raises(ScopeRefused, match="tasks is not served"):
        bind_resource(None, ["external_tasks"], served, SCOPE_OF)
    with pytest.raises(ResourceRefused):
        bind_resource(TASKS_URI, [], served, SCOPE_OF)


def test_without_a_tasks_scope_the_tasks_scope_string_is_unknown():
    two = {k: v for k, v in SCOPE_OF.items() if k is not MCPResource.TASKS}
    with pytest.raises(ScopeRefused, match="unknown or mixed"):
        bind_resource(None, ["external_tasks"], URIS, two)


def test_is_tasks_grant_names_only_the_tasks_resource():
    assert is_tasks_grant(TASKS_URI) and is_tasks_grant(TASKS_URI + "/")
    assert not is_tasks_grant(FACTS_URI) and not is_tasks_grant(DRIVE_URI) and not is_tasks_grant(None)
    assert not is_drive_grant(TASKS_URI)
