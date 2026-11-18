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
