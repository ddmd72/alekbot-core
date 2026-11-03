"""The drive connector's one-time download link route, `GET|HEAD /d/<link_id>` (DRIVE_MCP_CONNECTOR_RFC §8, E18).

Anonymous by design: the 128-bit id is the capability, and `DriveLinkService` re-checks the grant, the connection and
the access at every fetch. Not a reuse of `/f/<token>` (`file_access_app.py`: GCS-only, long-lived, a JWT).

- An id that is not 22 URL-safe characters answers 404 with no store read.
- HEAD (branched on `request.method` before anything: Quart would serve HEAD through the GET path, which consumes)
  runs every check, consumes nothing, reads no bytes: 200 with the file's headers and an empty body, or 404.
- GET consumes one read and streams the bytes through this service: never a redirect to the provider's URL (owner
  2026-11-03). The response has no `Content-Length` (chunked) and no Quart response timeout (`RESPONSE_TIMEOUT` is
  60 s); the Cloud Run request timeout caps one download.
- Every refusal (unknown, expired, spent, a revoked grant, a hidden drive, a folder out of access) is the same 404.
"""
from typing import AsyncIterator

from quart import Blueprint, Response, request

from ..domain.drive_link import LINK_ID_PATTERN, SAFETY_HEADERS, link_headers
from ..services.drive_link_service import DriveLinkService

NOT_VALID = "This link is not valid."


def _not_found() -> Response:
    """The one 404: plain text with the same safety headers as a 200 (the CSP sandbox included)."""
    return Response(NOT_VALID, status=404, mimetype="text/plain", headers=dict(SAFETY_HEADERS))


async def _empty() -> AsyncIterator[bytes]:
    return
    yield b""   # an async generator with no item: a HEAD body that sets no Content-Length


def create_drive_link_blueprint(links: DriveLinkService) -> Blueprint:
    bp = Blueprint("drive_link", __name__)

    @bp.route("/d/<link_id>", methods=["GET", "HEAD"])
    async def drive_link(link_id: str) -> Response:
        if not LINK_ID_PATTERN.match(link_id):
            return _not_found()
        if request.method == "HEAD":
            found = await links.head(link_id)
            if found is None:
                return _not_found()
            response = Response(_empty(), status=200, headers=link_headers(found.name, found.mime))
            response.timeout = None
            return response
        opened = await links.open(link_id)
        if opened is None:
            return _not_found()
        response = Response(opened.chunks, status=200, headers=link_headers(opened.name, opened.mime))
        response.timeout = None
        return response

    return bp
