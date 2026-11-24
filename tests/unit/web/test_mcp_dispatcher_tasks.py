"""The dispatcher and the tasks app (EXTERNAL_TASK_QUEUE_RFC §5.4): the tasks paths go to the tasks app before the
facts prefix match, `/mcp/consent` stays with Quart, a missing tasks app leaves its paths to Quart (404), and the
lifespan runs the tasks session manager."""
import contextlib

import pytest

from src.web import mcp_dispatcher
from src.web.mcp_dispatcher import build_shared_asgi

TASKS_PATHS = ("/tasks-mcp", "/tasks-mcp/", "/.well-known/oauth-protected-resource/tasks-mcp")


class _FakeMcp:
    def __init__(self, name, events):
        self.name, self.events = name, events
        self.session_manager = self

    @contextlib.asynccontextmanager
    async def run(self):
        self.events.append(f"{self.name} up")
        try:
            yield
        finally:
            self.events.append(f"{self.name} down")

    def streamable_http_app(self):
        async def app(scope, receive, send):
            self.events.append(f"{self.name} served {scope['path']}")
        return app


def _shared(*, tasks=True):
    events = []

    async def quart(scope, receive, send):
        events.append(f"quart served {scope['path']}")

    asgi = build_shared_asgi(quart, facts_mcp=_FakeMcp("facts", events), drive_mcp=_FakeMcp("drive", events),
                             tasks_mcp=_FakeMcp("tasks", events) if tasks else None)
    return events, asgi


async def _noop(*_):
    return None


@pytest.mark.parametrize("path", TASKS_PATHS)
def test_the_tasks_paths_go_to_the_tasks_app(path):
    assert mcp_dispatcher.mcp_owner(path) == "tasks"


def test_the_tasks_prm_wins_over_the_facts_prefix():
    path = "/.well-known/oauth-protected-resource/tasks-mcp"
    assert mcp_dispatcher._is_mcp_path(path) and mcp_dispatcher.mcp_owner(path) == "tasks"


@pytest.mark.parametrize("path", ["/mcp/consent", "/tasks-mcpx", "/tasks-mcp/sub", "/mcp/tasks-mcp"])
def test_neighbouring_paths_do_not_go_to_the_tasks_app(path):
    assert mcp_dispatcher.mcp_owner(path) != "tasks"


async def test_each_path_reaches_its_owner_with_the_tasks_app():
    events, asgi = _shared()
    for path in ("/tasks-mcp", "/.well-known/oauth-protected-resource/tasks-mcp", "/drive-mcp", "/mcp",
                 "/mcp/consent"):
        await asgi({"type": "http", "path": path}, _noop, _noop)
    assert events == ["tasks served /tasks-mcp", "tasks served /.well-known/oauth-protected-resource/tasks-mcp",
                      "drive served /drive-mcp", "facts served /mcp", "quart served /mcp/consent"]


async def test_without_the_tasks_app_its_paths_fall_through_to_quart():
    events, asgi = _shared(tasks=False)
    for path in TASKS_PATHS:
        await asgi({"type": "http", "path": path}, _noop, _noop)
    assert events == [f"quart served {path}" for path in TASKS_PATHS]


async def _run_lifespan(asgi):
    inbox = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]

    async def receive():
        return inbox.pop(0)

    await asgi({"type": "lifespan"}, receive, _noop)


async def test_the_lifespan_runs_the_tasks_session_manager():
    events, asgi = _shared()
    await _run_lifespan(asgi)
    assert events == ["facts up", "drive up", "tasks up", "tasks down", "drive down", "facts down"]


async def test_the_lifespan_without_the_tasks_app_runs_the_other_two():
    events, asgi = _shared(tasks=False)
    await _run_lifespan(asgi)
    assert events == ["facts up", "drive up", "drive down", "facts down"]
