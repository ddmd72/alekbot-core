"""
Sandbox app for the first pass of the two-pass first deploy (plan G9).

The service's own URL is the OIDC audience the gate pins, and it is unknown before the first deploy. With
`SANDBOX_URL` empty the service starts anyway so the deploy step succeeds: `GET /health` answers 200 and every other
request answers `503` with outcome `unconfigured`. No engine, gate or recycler exists on this path, so no request
can reach payload code and none recycles the instance.
"""
from __future__ import annotations

from quart import Blueprint, Response

from ..domain.sandbox_contract import OUTCOME_HEADER, SandboxOutcome, SandboxStatus, encode_outcome

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def create_unconfigured_blueprint() -> Blueprint:
    bp = Blueprint("sandbox_unconfigured", __name__)

    @bp.get("/health")
    async def health() -> Response:
        return Response("ok", status=200, content_type="text/plain")

    @bp.route("/", defaults={"path": ""}, methods=_METHODS)
    @bp.route("/<path:path>", methods=_METHODS)
    async def unconfigured(path: str) -> Response:
        resp = Response("unconfigured", status=503, content_type="text/plain; charset=utf-8")
        resp.headers[OUTCOME_HEADER] = encode_outcome(SandboxOutcome(status=SandboxStatus.UNCONFIGURED))
        return resp

    return bp
