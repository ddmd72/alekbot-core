"""main.mcp_owner: which MCP app (or the Quart app) a request path goes to."""

import contextlib

import pytest

import main


@pytest.mark.parametrize("path", [
    "/drive-mcp", "/drive-mcp/", "/.well-known/oauth-protected-resource/drive-mcp",
])
def test_drive_paths_go_to_the_drive_app(path):
    assert main.mcp_owner(path) == "drive"


@pytest.mark.parametrize("path", [
    "/mcp", "/mcp/", "/authorize", "/token", "/register", "/revoke",
    "/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource/mcp",
])
def test_facts_and_authorization_server_paths_go_to_the_facts_app(path):
    assert main.mcp_owner(path) == "facts"


@pytest.mark.parametrize("path", ["/mcp/consent", "/cabinet", "/drive-mcpx", "/drive-mcp/sub", "/health"])
def test_everything_else_goes_to_the_quart_app(path):
    assert main.mcp_owner(path) is None


def test_the_drive_prm_wins_over_the_facts_prefix():
    path = "/.well-known/oauth-protected-resource/drive-mcp"
    assert main._is_mcp_path(path)          # the facts matcher is a prefix match and would take it
    assert main.mcp_owner(path) == "drive"


# --------------------------------------------------------------------------- the dispatcher itself

class _FakeMcp:
    """Stands in for a FastMCP: a session manager that records start and stop, and an ASGI app that records calls."""

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


def _dispatcher(drive=True):
    events = []
    facts, drv = _FakeMcp("facts", events), (_FakeMcp("drive", events) if drive else None)

    async def quart(scope, receive, send):
        events.append(f"quart served {scope['path']}")

    async def facts_asgi(scope, receive, send):
        events.append(f"facts served {scope['path']}")

    return events, main.build_mcp_parent_asgi(main_app=quart, facts_mcp=facts, drive_mcp=drv, facts_asgi=facts_asgi)


async def _call(parent, path):
    await parent({"type": "http", "path": path}, None, None)


async def test_each_path_reaches_its_owner():
    events, parent = _dispatcher()
    for path in ("/mcp", "/drive-mcp", "/.well-known/oauth-protected-resource/drive-mcp", "/cabinet"):
        await _call(parent, path)
    assert events == ["facts served /mcp", "drive served /drive-mcp",
                      "drive served /.well-known/oauth-protected-resource/drive-mcp", "quart served /cabinet"]


async def test_a_drive_path_falls_through_to_quart_when_the_drive_app_did_not_build():
    events, parent = _dispatcher(drive=False)
    await _call(parent, "/drive-mcp")
    await _call(parent, "/mcp")
    assert events == ["quart served /drive-mcp", "facts served /mcp"]


async def _run_lifespan(parent):
    inbox = [{"type": "lifespan.startup"}, {"type": "lifespan.unknown"}, {"type": "lifespan.shutdown"}]
    sent = []

    async def receive():
        return inbox.pop(0)

    async def send(message):
        sent.append(message["type"])

    await parent({"type": "lifespan"}, receive, send)
    return sent


async def test_the_lifespan_runs_both_session_managers_and_stops_them_on_shutdown():
    events, parent = _dispatcher()
    sent = await _run_lifespan(parent)
    assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]   # an unknown message is ignored
    assert events == ["facts up", "drive up", "drive down", "facts down"]


async def test_the_lifespan_runs_only_the_facts_manager_without_the_drive_app():
    events, parent = _dispatcher(drive=False)
    await _run_lifespan(parent)
    assert events == ["facts up", "facts down"]


def test_a_failing_drive_build_leaves_none_and_the_facts_connector_alone(monkeypatch):
    def boom(**_):
        raise RuntimeError("drive build failed")

    monkeypatch.setattr(main, "build_drive_mcp_app", boom)
    assert main.build_drive_mcp_or_none(object(), object()) is None


def test_a_working_drive_build_is_returned(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(main, "build_drive_mcp_app", lambda **_: sentinel)
    assert main.build_drive_mcp_or_none(object(), object()) is sentinel
