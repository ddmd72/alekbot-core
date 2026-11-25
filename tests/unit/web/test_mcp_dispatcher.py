"""`build_shared_asgi`: the app the shared port serves, with and without the facts MCP connector.

The dispatcher's own routing and lifespan are covered by `tests/unit/test_mcp_dispatch.py`."""
from src.web.mcp_dispatcher import build_shared_asgi


class _FakeMcp:
    def __init__(self, name: str, events: list) -> None:
        self.name, self.events = name, events

    def streamable_http_app(self):
        async def app(scope, receive, send):
            self.events.append(f"{self.name} served {scope['path']}")
        return app


def _scope(path: str) -> dict:
    return {"type": "http", "path": path}


async def _noop(*_):
    return None


async def test_without_the_facts_connector_the_quart_app_is_served_as_is():
    async def quart(scope, receive, send):
        pass

    assert build_shared_asgi(quart, facts_mcp=None, drive_mcp=None) is quart


async def test_with_the_facts_connector_paths_go_to_their_owner():
    events: list = []

    async def quart(scope, receive, send):
        events.append(f"quart served {scope['path']}")

    asgi = build_shared_asgi(quart, facts_mcp=_FakeMcp("facts", events), drive_mcp=_FakeMcp("drive", events))
    for path in ("/mcp", "/drive-mcp", "/health"):
        await asgi(_scope(path), _noop, _noop)
    assert events == ["facts served /mcp", "drive served /drive-mcp", "quart served /health"]


async def test_a_drive_path_falls_through_to_quart_when_the_drive_app_did_not_build():
    events: list = []

    async def quart(scope, receive, send):
        events.append(f"quart served {scope['path']}")

    asgi = build_shared_asgi(quart, facts_mcp=_FakeMcp("facts", events), drive_mcp=None)
    await asgi(_scope("/drive-mcp"), _noop, _noop)
    assert events == ["quart served /drive-mcp"]


# ----------------------------------------------------------------------------- the memory inbox app (fourth resource)

def test_the_dispatcher_routes_the_inbox_paths():
    from src.web.mcp_dispatcher import mcp_owner
    assert [mcp_owner(p) for p in ("/memory-inbox-mcp", "/memory-inbox-mcp/",
                                   "/.well-known/oauth-protected-resource/memory-inbox-mcp", "/mcp")] == [
        "inbox", "inbox", "inbox", "facts"]
    assert [mcp_owner(p) for p in ("/memory-inbox-mcp/sub", "/memory-inbox-mcpx", "/mcp/memory-inbox-mcp")] == [
        None, None, None]


async def test_with_an_inbox_app_its_paths_reach_it():
    events: list = []

    async def quart(scope, receive, send):
        events.append(f"quart served {scope['path']}")

    asgi = build_shared_asgi(quart, facts_mcp=_FakeMcp("facts", events), drive_mcp=None,
                             inbox_mcp=_FakeMcp("inbox", events))
    for path in ("/memory-inbox-mcp", "/.well-known/oauth-protected-resource/memory-inbox-mcp", "/mcp"):
        await asgi(_scope(path), _noop, _noop)
    assert events == ["inbox served /memory-inbox-mcp",
                      "inbox served /.well-known/oauth-protected-resource/memory-inbox-mcp", "facts served /mcp"]


async def test_without_an_inbox_app_its_path_falls_to_quart():
    events: list = []

    async def quart(scope, receive, send):
        events.append(f"quart served {scope['path']}")

    asgi = build_shared_asgi(quart, facts_mcp=_FakeMcp("facts", events), drive_mcp=None, inbox_mcp=None)
    await asgi(_scope("/memory-inbox-mcp"), _noop, _noop)
    assert events == ["quart served /memory-inbox-mcp"]


async def test_the_lifespan_runs_the_inbox_session_manager():
    import contextlib
    events: list = []

    class Managed(_FakeMcp):
        def __init__(self, name: str) -> None:
            super().__init__(name, events)
            self.session_manager = self

        @contextlib.asynccontextmanager
        async def run(self):
            events.append(f"{self.name} up")
            yield
            events.append(f"{self.name} down")

    messages = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]

    async def receive():
        return messages.pop(0)

    async def quart(scope, receive, send):
        pass

    asgi = build_shared_asgi(quart, facts_mcp=Managed("facts"), drive_mcp=None, inbox_mcp=Managed("inbox"))
    await asgi({"type": "lifespan"}, receive, _noop)
    assert events == ["facts up", "inbox up", "inbox down", "facts down"]
