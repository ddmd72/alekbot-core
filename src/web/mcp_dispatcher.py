"""
The MCP ASGI dispatcher of the main service: which app (facts MCP, drive MCP, tasks MCP or the Quart app) serves a
path.

Starlette's `Mount("/mcp", ...)` strips the prefix and requires a trailing slash: it does NOT match an exact `/mcp`
for POST, which is what claude.ai sends. It also conflicts with the SDK's absolute-path routes (the SDK builds
`/authorize`, `/token`, `/register` and the protected-resource metadata at the server root, not under a mount
prefix). So a plain ASGI dispatcher forwards the FastMCP-owned paths to the SDK sub-app WITHOUT path rewriting, and
everything else falls through to the Quart app (`/mcp/consent`, `/auth/*`, `/api/*`, `/slack/*`, `/worker`,
`/health`, `/cabinet`). Without a facts MCP connector the Quart app is served as is (the drive and tasks apps sit behind
the facts app's dispatcher). An app whose build failed (None) leaves its paths to the Quart app, which answers 404.
"""
import contextlib
from typing import Optional

from ..domain.mcp_resource import DRIVE_PATH, TASKS_PATH
from .mcp_error_logger import McpErrorLogger


def _is_mcp_path(path: str) -> bool:
    # The paths the facts app (authorization server + `/mcp`) serves. No docstring: a test reads this body's literals.
    if path == "/mcp" or path == "/mcp/":
        return True
    if path in ("/authorize", "/token", "/register", "/revoke"):
        return True
    if path == "/.well-known/oauth-authorization-server":
        return True
    if path.startswith("/.well-known/oauth-protected-resource"):
        return True
    return False


def mcp_owner(path: str) -> Optional[str]:
    """Which MCP app owns `path`: "tasks", "drive", "facts", or None (the Quart app).

    The tasks and drive checks run first: the facts protected-resource-metadata test is a prefix match.
    """
    if path in (TASKS_PATH, TASKS_PATH + "/"):
        return "tasks"
    if path == "/.well-known/oauth-protected-resource" + TASKS_PATH:
        return "tasks"
    if path in (DRIVE_PATH, DRIVE_PATH + "/"):
        return "drive"
    if path == "/.well-known/oauth-protected-resource" + DRIVE_PATH:
        return "drive"
    return "facts" if _is_mcp_path(path) else None


def build_mcp_parent_asgi(*, main_app, facts_mcp, drive_mcp, facts_asgi, tasks_mcp=None):
    """The plain ASGI dispatcher: lifespan runs every MCP session manager; paths go by `mcp_owner`.

    `drive_mcp` None (its build failed): `/drive-mcp` falls through to the Quart app (404); `tasks_mcp` None (no task
    queue, or its build failed): `/tasks-mcp` likewise.
    """
    drive_asgi = McpErrorLogger(drive_mcp.streamable_http_app()) if drive_mcp is not None else None
    tasks_asgi = McpErrorLogger(tasks_mcp.streamable_http_app()) if tasks_mcp is not None else None

    async def parent_asgi(scope, receive, send):
        if scope["type"] == "lifespan":
            async with contextlib.AsyncExitStack() as stack:
                await stack.enter_async_context(facts_mcp.session_manager.run())
                if drive_mcp is not None:
                    await stack.enter_async_context(drive_mcp.session_manager.run())
                if tasks_mcp is not None:
                    await stack.enter_async_context(tasks_mcp.session_manager.run())
                while True:
                    message = await receive()
                    if message["type"] == "lifespan.startup":
                        await send({"type": "lifespan.startup.complete"})
                    elif message["type"] == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        return
        owner = mcp_owner(scope.get("path", ""))
        if owner == "facts":
            await facts_asgi(scope, receive, send)
        elif owner == "drive" and drive_asgi is not None:
            await drive_asgi(scope, receive, send)
        elif owner == "tasks" and tasks_asgi is not None:
            await tasks_asgi(scope, receive, send)
        else:
            await main_app(scope, receive, send)

    return parent_asgi


def build_shared_asgi(main_app, *, facts_mcp, drive_mcp, tasks_mcp=None):
    """The ASGI app the shared port serves: the dispatcher when the facts MCP connector is built, else `main_app`."""
    if facts_mcp is None:
        return main_app
    return build_mcp_parent_asgi(
        main_app=main_app, facts_mcp=facts_mcp, drive_mcp=drive_mcp,
        facts_asgi=McpErrorLogger(facts_mcp.streamable_http_app()), tasks_mcp=tasks_mcp,
    )
