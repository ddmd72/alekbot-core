"""Shared helpers for the sandbox client tests: a scripted HTTP transport and the real-app ASGI transport."""
from typing import Callable, List, Optional

import httpx
from quart import Quart

from src.domain import sandbox_contract as sc
from src.domain.sandbox_contract import SandboxLimits, SandboxOp, SandboxOutcome, SandboxStatus
from src.web import sandbox_app
from tests.unit.web.test_sandbox_app import CEILING, FakeEngine, FakeGate, FakeRecycler

BASE = "http://sandbox.test"
LIMITS = SandboxLimits(wall_s=60.0, memory_bytes=1 << 30, cpu_s=30.0, file_bytes=1 << 20)


async def good_token() -> str:
    return "good"


def outcome_header(status: SandboxStatus, detail: str = "") -> str:
    return sc.encode_outcome(SandboxOutcome(status=status, detail=detail))


def reply(code: int, status: Optional[SandboxStatus] = None, body: bytes = b"", detail: str = "") -> httpx.Response:
    headers = {sc.OUTCOME_HEADER: outcome_header(status, detail)} if status is not None else {}
    return httpx.Response(code, headers=headers, content=body)


class Scripted(httpx.AsyncBaseTransport):
    """Answers each request from a script of responses or exceptions and records the requests."""

    def __init__(self, *script) -> None:
        self.script: List = list(script)
        self.requests: List[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(request)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Callable) and not isinstance(item, httpx.Response):
            item = item(request)
        if isinstance(item, BaseException):
            raise item
        return item


def real_app_transport(tmp_path, engine=None, recycle="never", gate=None, ceiling=CEILING) -> httpx.ASGITransport:
    engine = engine or FakeEngine(tmp_path)
    out_root = getattr(engine, "out_root", None) or (tmp_path / "engine-out")
    work_root = tmp_path / "work"
    work_root.mkdir(exist_ok=True)
    app = Quart(__name__)
    app.register_blueprint(sandbox_app.create_sandbox_blueprint(
        {SandboxOp.RENDER_DOCX: engine}, gate or FakeGate(), FakeRecycler(), ceiling,
        {"work_root": str(work_root), "engine_user": None, "recycle": recycle,
         "output_root": str(out_root)}))
    transport = httpx.ASGITransport(app=app)
    transport.engine = engine  # type: ignore[attr-defined]
    return transport
