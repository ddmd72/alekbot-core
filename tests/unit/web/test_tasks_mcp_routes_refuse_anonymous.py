"""Every route of the tasks MCP app (`/tasks-mcp`, EXTERNAL_TASK_QUEUE_RFC §5.4) except the public protected-resource
metadata refuses an anonymous caller with 401, reads no key and writes nothing.

The facts app's probe (`test_every_route_refuses_anonymous.py`) pins `_is_mcp_path`'s literals, the facts app's own;
the drive and tasks apps are their own SDK apps behind `mcp_owner`, so each has its own probe (the drive one is
`test_drive_mcp_routes_refuse_anonymous.py`). The app is the one `tests/support/task_mcp_world.py` builds.
"""

import httpx

from src.domain.mcp_resource import TASKS_PATH
from tests.support.task_mcp_world import BASE, World


async def _world() -> World:
    w = await World().seed()
    w.initial = await w.texts.texts()
    return w


def _routes(asgi):
    out = []
    for route in asgi.routes:
        methods = route.methods or {"GET", "POST", "DELETE"}    # the endpoint is a raw ASGI mount: all three
        out += [(m, route.path) for m in sorted(methods - {"HEAD", "OPTIONS"})]
    return out


async def test_the_tasks_app_serves_the_endpoint_and_only_public_metadata_besides():
    paths = {path for _, path in _routes((await _world()).app().streamable_http_app())}
    assert TASKS_PATH in paths
    assert {p for p in paths if p != TASKS_PATH} == {"/.well-known/oauth-protected-resource" + TASKS_PATH}


async def test_every_tasks_route_but_the_metadata_refuses_an_anonymous_caller_and_writes_nothing():
    world = await _world()
    await world.create()
    tasks_before = {k: t.status for k, t in world.tasks.tasks.items()}
    app = world.app()
    asgi = app.streamable_http_app()
    probes = [(m, p) for m, p in _routes(asgi) if not p.startswith("/.well-known/")]
    assert probes
    failures = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi, raise_app_exceptions=False),
                                 base_url=BASE) as http:
        async with app.session_manager.run():
            for method, path in probes:
                resp = await http.request(method, path)
                if resp.status_code != 401:
                    failures.append(f"{method} {path}: answered {resp.status_code}")
    assert failures == []
    assert world.workers.get_key_reads == 0
    assert world.rate_docs() == 0
    assert {k: t.status for k, t in world.tasks.tasks.items()} == tasks_before
