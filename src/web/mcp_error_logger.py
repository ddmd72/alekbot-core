from typing import Any, Awaitable, Callable, Dict

from src.utils.logger import logger

_MCP_PATHS = ("/mcp", "/mcp/")
_BODY_CAP = 512
_QUIET_STATUSES = (401,)

ASGIApp = Callable[[Dict[str, Any], Callable[..., Awaitable[Any]], Callable[..., Awaitable[Any]]], Awaitable[None]]


class McpErrorLogger:
    """ASGI wrapper: records why /mcp answered with a 4xx.

    The MCP SDK explains a rejection only in the response body, and Cloud Run's request log
    carries the status alone. Authorization is never logged; the session id only as present/absent.
    """

    def __init__(self, app: ASGIApp):
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") not in _MCP_PATHS:
            await self._app(scope, receive, send)
            return

        status = 0
        body = bytearray()

        async def send_and_watch(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body" and self._is_rejection(status):
                room = _BODY_CAP - len(body)
                if room > 0:
                    body.extend(message.get("body", b"")[:room])
            await send(message)
            if (
                message["type"] == "http.response.body"
                and not message.get("more_body")
                and self._is_rejection(status)
            ):
                self._log(scope, status, bytes(body))

        await self._app(scope, receive, send_and_watch)

    @staticmethod
    def _is_rejection(status: int) -> bool:
        return status >= 400 and status not in _QUIET_STATUSES

    @staticmethod
    def _log(scope, status: int, body: bytes) -> None:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        logger.info(
            "mcp_request_rejected status=%s method=%s protocol_version=%s has_session_id=%s "
            "content_type=%s accept=%s body=%s",
            status,
            scope.get("method"),
            headers.get("mcp-protocol-version"),
            "mcp-session-id" in headers,
            headers.get("content-type"),
            headers.get("accept"),
            body.decode("utf-8", "replace"),
        )
